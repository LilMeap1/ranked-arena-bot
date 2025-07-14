import trueskill
from pymongo import MongoClient
import random
import datetime


env = trueskill.TrueSkill(mu=1000, sigma=300, beta=200, tau=0.05, draw_probability=0.0)


MONGO_URI = "" # removed for public view purposes
client = MongoClient(MONGO_URI)
db = client["Ranked-Arena-Database"]

def update_player_mmr(player_id, new_mu, new_sigma, result):
    """
    Update player MMR and stats in MongoDB, with progressive MMR decay and sigma clamping.
    """
    try:
        query_id = int(player_id)
    except ValueError:
        print(f"Warning: Could not convert player_id {player_id} to int for query.")
        return False

    user_doc = db.users.find_one({'discord_id': query_id})

    if not user_doc:
        print(f"⚠️ Player with ID {player_id} does not exist!")
        return False

    data = user_doc
    current_mmr = data.get('mmr', 1000)
    previous_sigma = data.get('confidence', 300)
    games_played = data.get('games_played', 0)


    manual_sigma = max(100, 300 - games_played * 3)
    if new_sigma < previous_sigma:
        new_sigma = max(manual_sigma, new_sigma)
    else:
        new_sigma = previous_sigma


    raw_delta = new_mu - current_mmr
    decay_factor = max(0.3, 1 - (games_played * 0.02))
    adjusted_delta = raw_delta * decay_factor

    if abs(adjusted_delta) < 20:
        adjusted_delta = 20 + random.uniform(0, 5)
        adjusted_delta = adjusted_delta if result == 'win' else -adjusted_delta

    adjusted_delta = max(-70, min(70, adjusted_delta))

    new_mmr = current_mmr + adjusted_delta

    update_data = {
        'mmr': new_mmr,
        'confidence': new_sigma,
        'games_played': games_played + 1
    }

    if result == 'win':
        update_data['wins'] = data.get('wins', 0) + 1
    elif result == 'lose':
        update_data['losses'] = data.get('losses', 0) + 1

    db.users.update_one({'discord_id': query_id}, {'$set': update_data})

    print(f"✅ Player {player_id}: ΔMMR = {adjusted_delta:.1f}, New MMR = {new_mmr:.1f}, σ = {new_sigma:.2f} (↓ {previous_sigma - new_sigma:.2f})")
    return {
        "discord_id": player_id,
        "delta": adjusted_delta,
        "new_mmr": new_mmr
    }

def adjust_mmr_for_game(team_a, team_b, result):
    mmr_changes = [] 

    def fetch_user_rating(discord_id):
        try:
            query_id = int(discord_id)
        except ValueError:
            print(f"Warning: Could not convert discord_id {discord_id} to int for query.")
            return env.create_rating(mu=1000, sigma=300)

        user_doc = db.users.find_one({'discord_id': query_id})
        if not user_doc:
            print(f"⚠️ User {discord_id} not found in DB!")
            return env.create_rating(mu=1000, sigma=300)
        mu = user_doc.get('mmr', 1000)
        sigma = user_doc.get('confidence', 300)
        return env.create_rating(mu=mu, sigma=sigma)

    team_a_ratings = [fetch_user_rating(p['discord_id']) for p in team_a]
    team_b_ratings = [fetch_user_rating(p['discord_id']) for p in team_b]

    if result == "team_a":
        team_a_result, team_b_result = env.rate([team_a_ratings, team_b_ratings], ranks=[0, 1])
    else:
        team_a_result, team_b_result = env.rate([team_a_ratings, team_b_ratings], ranks=[1, 0])

    for player, new_rating in zip(team_a, team_a_result):
        update = update_player_mmr(player['discord_id'], new_rating.mu, new_rating.sigma, 'win' if result == "team_a" else 'lose')
        if update:
            update["ign"] = player["ign"]
            mmr_changes.append(update)

    for player, new_rating in zip(team_b, team_b_result):
        update = update_player_mmr(player['discord_id'], new_rating.mu, new_rating.sigma, 'win' if result == "team_b" else 'lose')
        if update:
            update["ign"] = player["ign"]
            mmr_changes.append(update)

    return mmr_changes

def process_match_result(game_id, result):
    game_doc = db.games.find_one({'_id': game_id})

    if not game_doc:
        print(f"⚠️ Game {game_id} not found!")
        return None, None

    team_a = game_doc.get('team_a', [])
    team_b = game_doc.get('team_b', [])

    mmr_changes_raw = adjust_mmr_for_game(team_a, team_b, result)
    mmr_changes = []

    team_a_igns = {str(player.get('ign')).lower() for player in team_a}
    team_b_igns = {str(player.get('ign')).lower() for player in team_b}

    for entry in mmr_changes_raw:
        ign = str(entry.get('ign')).lower()
        player_dict = dict(entry)

        if ign in team_a_igns:
            player_dict['team'] = 'team_a'
        elif ign in team_b_igns:
            player_dict['team'] = 'team_b'
        else:
            print(f"[WARN] IGN '{entry.get('ign')}' not found in either team for game {game_id}")
            player_dict['team'] = 'unknown'
        mmr_changes.append(player_dict)

    return result, mmr_changes