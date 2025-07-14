from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time
import urllib.parse
import hashlib
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException 
from pymongo import MongoClient

MONGO_URI = "" # removed for public view purposes
client = MongoClient(MONGO_URI)
db = client["Ranked-Arena-Database"]

def init_browser():
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    return webdriver.Chrome(options=options)

def robust_get(driver, url, max_retries=3, wait_seconds=5):
    for attempt in range(max_retries):
        try:
            driver.get(url)
            return True 
        except TimeoutException as e:
            print(f"[WARN] Timeout on attempt {attempt+1}/{max_retries} for {url}")
            try:
                driver.save_screenshot(f"selenium_timeout_attempt_{attempt+1}.png")
            except Exception:
                pass
            if attempt < max_retries - 1:
                time.sleep(wait_seconds)
            else:
                raise 
    return False

def store_user_id_if_needed(ign):
    user_doc = db.users.find_one({'ign': ign})
    print(f"[DEBUG] IGN received: '{ign}' (type: {type(ign)})")
    if not user_doc:
        print(f"⚠️ IGN {ign} not found in users collection.")
        return None

    if 'user_id' in user_doc and user_doc['user_id']:
        print(f"✅ User ID already exists for {ign}: {user_doc['user_id']}")
        return user_doc['user_id']

    driver = init_browser()
    driver.get("https://supervive-stats.com")
    time.sleep(2)
    accept_consent_popup(driver)
    time.sleep(1)

    input_box = WebDriverWait(driver, 30).until(
        EC.visibility_of_element_located((By.XPATH, "//input[@placeholder='Player#0000']"))
    )
    driver.execute_script("arguments[0].scrollIntoView();", input_box)
    time.sleep(0.5)
    input_box.clear()
    if not ign:
        print("❌ IGN is empty or undefined. Aborting input.")
        driver.quit()
        return None
    driver.save_screenshot('debug_input_box.png')
    input_box.send_keys(ign)

    time.sleep(5)

    dropdown_options = driver.find_elements(
        By.CSS_SELECTOR,
        "li.flex.cursor-pointer.items-center")
    if not dropdown_options:
        print("⚠️ No search results found.")
        driver.quit()
        return None

    dropdown_options[0].click()
    time.sleep(2)

    current_url = driver.current_url
    driver.quit()

    user_id_from_url = current_url.split("/players/")[-1]

    if 'discord_id' in user_doc:

        try:
            discord_id_for_update = int(user_doc['discord_id'])
        except ValueError:
            print(f"Warning: Could not convert discord_id {user_doc['discord_id']} to int for update. Using original value.")
            discord_id_for_update = user_doc['discord_id']

        db.users.update_one({'discord_id': discord_id_for_update}, {'$set': {'user_id': user_id_from_url}})
        print(f"✅ Stored user_id {user_id_from_url} for {ign}")
        return user_id_from_url
    else:
        print(f"❌ Cannot update user_id for {ign} as discord_id is missing in the document.")
        return None


def accept_consent_popup(driver):
    try:
        consent_button = WebDriverWait(driver, 2).until(
            EC.presence_of_element_located((By.XPATH, "//p[contains(@class, 'fc-button-label') and text()='Consent']"))
        )
        if consent_button.is_displayed():
            try:
                parent_btn = consent_button.find_element(By.XPATH, "./ancestor::button | ./ancestor::div[@role='button']")
                parent_btn.click()
                print("[INFO] Consent pop-up found and clicked.")
                time.sleep(1)
            except Exception as click_e:
                print(f"[WARN] Consent pop-up found but could not be clicked: {click_e}")
        else:
            print("[INFO] Consent pop-up present but not visible, skipping.")
    except TimeoutException:
        print("[INFO] Consent pop-up not found, continuing...")
    except NoSuchElementException:
        print("[INFO] Consent pop-up not found, continuing...")
    except Exception as e:
        print(f"[ERROR] Unexpected error in consent pop-up handling: {e}")

def get_latest_custom_game(driver, game_id):
    try:
        time.sleep(2)
        accept_consent_popup(driver)
        time.sleep(2)
        match_history_containers = driver.find_elements(By.CSS_SELECTOR, "div.flex.flex-col.gap-2")
        if len(match_history_containers) < 3:
            print("❌ Match history container not found!")
            return None, None

        game_cards = match_history_containers[2].find_elements(By.XPATH, "./div")
        if not game_cards:
            print("❌ No match cards found in match history!")
            return None, None

        latest_card = game_cards[0]
        game_text = latest_card.text
        print("[DEBUG] Single game card text:", repr(game_text))

        if "Custom" not in game_text:
            print("⚠️ Last game is not a Custom game.")
            time.sleep(60)
            return None, None

        try:
            stamp_div = latest_card.find_element(By.XPATH, ".//div[contains(@class,'flex') and contains(@class,'gap-1') and contains(@class,'text-muted-foreground') and contains(@class,'text-sm')]")
            all_spans = stamp_div.find_elements(By.TAG_NAME, "span")
            timestamp_element = all_spans[-1]
            timestamp_text = timestamp_element.text.strip()
        except Exception as e:
            print(f"[DEBUG] Could not extract timestamp span cleanly: {e}")
            return None, None

        print(f"[DEBUG] Timestamp extracted: '{timestamp_text}'")
        game_doc = db.games.find_one({'_id': game_id})
        if game_doc and game_doc.get("game_type") == "draft_arena":
            if (
                "Yesterday" in timestamp_text
                or "hour" in timestamp_text
                or "day" in timestamp_text
                or (timestamp_text.endswith("minutes ago") and int(timestamp_text.split()[0]) > 7)
                or (timestamp_text.endswith("minute ago") and int(timestamp_text.split()[0]) > 7)
            ):
                print(f"⏳ Game is too old: {timestamp_text}")
                time.sleep(60)
                return None, None
        else:
            if (
                "Yesterday" in timestamp_text
                or "hour" in timestamp_text
                or "day" in timestamp_text
                or (timestamp_text.endswith("minutes ago") and int(timestamp_text.split()[0]) > 5)
                or (timestamp_text.endswith("minute ago") and int(timestamp_text.split()[0]) > 5)
            ):
                print(f"⏳ Game is too old: {timestamp_text}")
                time.sleep(60)
                return None, None


        driver.execute_script("arguments[0].remove();", timestamp_element)
        cleaned_text = latest_card.text
        print(f"Cleaned_text: '{cleaned_text}")
        game_hash = hashlib.sha256(cleaned_text.encode('utf-8')).hexdigest()

        return game_text, game_hash
    except Exception as e:
        print(f"ERROR in get_latest_custom_game: {e}")
        return None, None

def monitor_game_v2(ign, game_id, team):
    game_doc = db.games.find_one({'_id': game_id})
    if game_doc and game_doc.get("game_type") == "draft_arena":
        print("Draft Arena detected, sleeping 8 minutes before monitoring...")
        time.sleep(480)
    else:
        print("Normal game type, sleeping 5 minutes before monitoring...")
        time.sleep(300)

    user_id = store_user_id_if_needed(ign)
    if not user_id:
        return

    driver = init_browser()
    try:
        robust_get(driver, f"https://supervive-stats.com/players/{user_id}", max_retries=20, wait_seconds=15)
    except TimeoutException:
        print(f"[ERROR] All attempts to load player page for {user_id} failed. Skipping this IGN.")
        driver.quit()
        return
    time.sleep(5)

    start_time = time.time()
    max_wait_seconds = 1800

    while True:
        if time.time() - start_time > max_wait_seconds:
            db.games.update_one({'_id': game_id}, {'$set': {'result': 'timed_out'}})
            print(f"⏰ Game '{game_id}' monitoring timed out after 30 minutes.")
            driver.quit()
            break

        game_doc = db.games.find_one({'_id': game_id})
        if game_doc and game_doc.get('result') == 'canceled':
            print(f"Game '{game_id}' was canceled by vote. Exiting monitor.")
            driver.quit()
            break

        game_text, game_hash = get_latest_custom_game(driver, game_id)
        if not game_text or not game_hash:
            print("⏳ No valid custom game yet. Refreshing page...")
            driver.refresh()
            time.sleep(10)
            continue

        existing = db.games.find_one({"block_hash": game_hash, "_id": {"$ne": game_id}})
        if existing:
            print("⚠️ Game already processed.")
            driver.refresh()
            time.sleep(10)
            continue

        result = "team_a" if "1st" in game_text else "team_b" if "2nd" in game_text else "unknown"
        if result in ["team_a", "team_b"]:
            db.games.update_one({'_id': game_id}, {'$set': {'result': result, 'block_hash': game_hash}})
            print(f"✅ Game '{game_id}' updated: {result}")
        else:
            print("⚠️ No result found.")
        break

    driver.quit()