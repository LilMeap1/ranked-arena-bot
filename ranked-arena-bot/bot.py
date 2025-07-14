import discord 
from discord.ext import commands, tasks
from discord import app_commands, Thread
from config import config
import asyncio
import threading
from game_monitor_v2 import monitor_game_v2
from mmr_manager import process_match_result
from discord import Interaction, ui
from datetime import datetime, timezone, timedelta
from itertools import combinations
import pymongo
from pymongo import MongoClient
import uuid
import random

GUILD_ID = 1278865926975918100

ephemeral_tracker = {}

HUNTERS = [
    "Brall", "Carbine", "Crysta", "Ghost", "Jin",
    "Joule", "Myth", "Saros", "Shiv", "Shrike",
    "Bishop", "Kingpin", "Felix", "Oath", "Elluna",
    "Eva", "Zeph", "Beebo", "Celeste", "Hudson",
    "Void"
]

MONGO_URI = "" # removed for public view purposes
client = MongoClient(MONGO_URI)
db = client["Ranked-Arena-Database"]

last_access_ui_message = None
ALLOWED_CHANNEL_ID = 1374850765830754446
ALLOWED_ROLES = {"New Tech", "Admin", "Owner", "Helper guy"}
ANNOUNCE_CHANNEL_ID = 1377002789930143804

intents = discord.Intents.default()

bot = commands.Bot(command_prefix='/', intents=intents)

def get_user_data(discord_id):
    return db.users.find_one({"discord_id": discord_id})

def get_user_data_by_ign(ign):
    return db.users.find_one({"ign": ign})

def process_vote_stop(user_id, game_id):
    game = db.games.find_one({"_id": game_id})
    if not game:
        return False, f"{game_id} not found."
    if game.get('result') in ['canceled', 'processed', 'timed_out', 'team_a', 'team_b']:
        return False, f"{game_id} is already finished or canceled."
    
    allowed_voters = {str(p.get('discord_id')) for p in game.get('team_a', []) + game.get('team_b', [])}
    if str(user_id) not in allowed_voters:
        return False, "Only players in this game can vote to cancel it."

    votes = game.get('votes', [])
    if user_id in votes:
        return False, "You've already voted to stop this game."
    
    votes.append(user_id)
    db.games.update_one({"_id": game_id}, {"$set": {"votes": votes}})
    
    if len(votes) >= 6:
        db.games.update_one({"_id": game_id}, {"$set": {"result": "canceled"}})
        return True, f"{game_id} has been canceled by vote (6/8 or more players agreed). No MMR has been changed."

    return True, f"Your vote was counted. {len(votes)}/8 players have voted to cancel {game_id}. (Need 6 total)"

def check_channel(ctx):
    return ctx.channel.id == ALLOWED_CHANNEL_ID

def has_permission(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.name in ALLOWED_ROLES for role in interaction.user.roles)

def add_to_queue(discord_id, game_type="ranked_arena"):
    user = get_user_data(discord_id)
    if not user:
        return False
    
    if db.in_queue.find_one({"discord_id": discord_id}):
        return False, "already_in_queue"

    if db.in_queue.find_one({"discord_id": discord_id}) or db.games.find_one({"players.discord_id": discord_id, "result": {"$in": ["pending", "team_a", "team_b"]}}):
        return False
    
    db.in_queue.insert_one({
        'discord_id': discord_id,
        'ign': user['ign'],  
        'mmr': user.get('mmr', 1000), 
        'confidence': user.get('confidence', 300), 
        'games_played': user.get('games_played', 0),
        'wins': user.get('wins', 0),
        'losses': user.get('losses', 0),
        'queue_joined_at': datetime.now(timezone.utc),
        'game_type': game_type
    })
    return True, "added"

def remove_from_queue(discord_id):
    result = db.in_queue.delete_one({"discord_id": discord_id})
    return result.deleted_count > 0

def move_to_ingame(discord_id, game_id, team):
    user = get_user_data(discord_id)
    if not user:
        return False
    
    player_data = {
        'discord_id': discord_id,
        'ign': user['ign'],
        'mmr': user.get('mmr', 1000),
        'confidence': user.get('confidence', 300),
        'games_played': user.get('games_played', 0),
        'wins': user.get('wins', 0),
        'losses': user.get('losses', 0)
    }

    db.games.update_one({"_id": game_id}, {"$addToSet": {team: player_data}})
    return True

def create_user(discord_id, ign_tag):
    if get_user_data(discord_id):
        return None
    
    user_data = {
        'discord_id': discord_id,
        'ign': ign_tag,
        'mmr': 1000,
        'confidence': 300,
        'games_played': 0,
        'wins': 0,
        'losses': 0
    }
    db.users.insert_one(user_data)
    return user_data

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'Logged in as {bot.user}')
    check_queue.start()
    check_and_update_results.start()
    channel = bot.get_channel(ALLOWED_CHANNEL_ID)
    embed = get_queue_status_embed()
    await post_access_ui_message(channel, embed=embed)
    refresh_access_ui_message.start()
    update_access_ui_embed.start()
    cleanup_old_draft_threads.start()
    
class ConfirmClearView(ui.View):
    def __init__(self):
        super().__init__(timeout=30)
        self.value = None

    @ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()

@bot.tree.command(name="clear_channel", description="Delete all messages in this channel (last 14 days).")
@app_commands.default_permissions(manage_messages=True)
async def clear_channel(interaction: discord.Interaction):
    if not hasattr(interaction.channel, "purge"):
        await interaction.response.send_message("Cannot purge messages in this type of channel.", ephemeral=True)
        return

    view = ConfirmClearView()
    await interaction.response.send_message(
        "⚠️ **Are you sure you want to delete up to 1000 recent messages in this channel?**",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value is None:
        await interaction.followup.send("⏱️ No response. Cancelled.", ephemeral=True)
    elif view.value:
        deleted = await interaction.channel.purge(limit=1000, bulk=True)
        await interaction.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Cancelled.", ephemeral=True)

@bot.tree.command(name="create_user", description="Register a new user with your in-game name.")
async def create_user_command(interaction: discord.Interaction, ign_tag: str):
    if not check_channel(interaction):
        await interaction.response.send_message("This command can only be used in the specified channel.", ephemeral=True)
        return

    user = create_user(interaction.user.id, ign_tag)
    if user:
        await interaction.response.send_message(f'User {ign_tag} created for {interaction.user}.', ephemeral=True)
    else:
        await interaction.response.send_message(f'You already have a user profile, {interaction.user}.', ephemeral=True)


@bot.tree.command(name="edit_ign", description="Edit your in-game name (IGN).")
async def edit_ign_command(interaction: discord.Interaction, new_ign: str):
    if not check_channel(interaction):
        await interaction.response.send_message(
            "This command can only be used in the specified channel.",
            ephemeral=True
        )
        return

    user = get_user_data(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            f"No user profile found. Please register first with `/create_user`.", ephemeral=True
        )
    else:
        db.users.update_one({"discord_id": interaction.user.id}, {"$set": {'ign': new_ign}})
        await interaction.response.send_message(
            f"Your IGN has been updated to `{new_ign}`.", ephemeral=True
        )

@bot.tree.command(name="my_data", description="View your own user data.")
async def my_data_command(interaction: discord.Interaction):
    if not check_channel(interaction):
        await interaction.response.send_message("This command can only be used in the specified channel.", ephemeral=True)
        return

    user = get_user_data(interaction.user.id)
    if not user:
        await interaction.response.send_message(f'No data found. Please register first.', ephemeral=True)
        return

    await interaction.response.send_message(f"**Your Data**\n"
        f"MMR: {user['mmr']}\n"
        f"Wins: {user['wins']}\n"
        f"Losses: {user['losses']}\n"
        f"Games Played: {user['games_played']}", ephemeral=True)
    
@bot.tree.command(name="user_data", description="View data of another user (by IGN).")
async def user_data_command(interaction: discord.Interaction, ign: str):
    if not check_channel(interaction):
        await interaction.response.send_message("This command can only be used in the specified channel.", ephemeral=True)
        return

    user = get_user_data_by_ign(ign)
    if not user:
        await interaction.response.send_message(f'No user found with the IGN {ign}.', ephemeral=True)
        return

    await interaction.response.send_message(f"**{ign}'s Data**\n"
        f"MMR: {user['mmr']}\n"
        f"Wins: {user['wins']}\n"
        f"Losses: {user['losses']}\n"
        f"Games Played: {user['games_played']}", ephemeral=True)

@bot.tree.command(name="add_test_players", description="Add 8 test players to the queue for debugging/testing purposes.")
async def add_test_players_command(interaction: discord.Interaction):
    if not check_channel(interaction):
        await interaction.response.send_message("This command can only be used in the specified channel.", ephemeral=True)
        return
    
    if not has_permission(interaction):
        await interaction.response.send_message(
            "You don't have the required permissions to use this command", ephemeral=True)
        return

    test_players = [
        ("Kask#3160", 1000),
        ("Furotiza#00", 1000),
        ("fallfromgrace#luca", 1000),
        ("LilMeap#0001", 1000),
        ("Mythi#BOMB", 1000),
        ("blink#1337", 1000),
        ("Cookiess66#liv1", 1000),
        ("TTV_yaserAQ#0000", 1000)
    ]

    for ign, mmr in test_players:
        discord_id = f"test_{ign}"

        if db.in_queue.find_one({"discord_id": discord_id}):
            print(f"Player {ign} is already in the queue.")
            continue

        db.in_queue.insert_one({
            'discord_id': discord_id,
            'ign': ign,
            'mmr': mmr,
            'confidence': 300,
            'games_played': 0,
            'wins': 0,
            'losses': 0,
            'queue_joined_at': datetime.now(timezone.utc),
            'game_type': 'draft_arena'
        })

        print(f"Added {ign} with {mmr} MMR to the queue.")

    await interaction.response.send_message("Added 8 test players to the queue for debugging/testing.", ephemeral=True)

@bot.tree.command(name="add_test_users", description="Add test users to the users collection for debugging/testing purposes.")
async def add_test_users_command(interaction: discord.Interaction):
    if not check_channel(interaction):
        await interaction.response.send_message("This command can only be used in the specified channel.", ephemeral=True)
        return

    if not has_permission(interaction):
        await interaction.response.send_message(
            "You don't have the required permissions to use this command", ephemeral=True)
        return    
    
    test_players = [
        ("test_Kask#3160", 1000),
        ("test_Furotiza#00", 1000),
        ("test_fallfromgrace#luca", 1000),
        ("test_LilMeap#0001", 1000),
        ("test_Mythi#BOMB", 1000),
        ("test_blink#1337", 1000),
        ("test_Cookiess66#liv1", 1000),
        ("test_TTV_yaserAQ#0000", 1000)
    ]

    for ign, mmr in test_players:
        discord_id = f"{ign}"

        if db.users.find_one({"discord_id": discord_id}):
            print(f"Player {ign} already exists.")
            continue

        db.users.insert_one({
            'discord_id': discord_id,
            'ign': ign,
            'mmr': mmr,
            'confidence': 300,
            'games_played': 0,
            'wins': 0,
            'losses': 0
        })

        print(f"Added {ign} with {mmr} MMR to the users collection.")

    await interaction.response.send_message("Added test users to the users collection for debugging/testing.", ephemeral=True)


async def start_matchmaking(players_in_queue_for_type, bot):
    players = sorted(
        players_in_queue_for_type, 
        key=lambda x: x.get('queue_joined_at', datetime.min)
    )

    if len(players) < 8:
        return None, None, None, None

    game_type = players[0]['game_type']
    if any(p['game_type'] != game_type for p in players):
        print("[ERROR] Mixed game types in matchmaking pool. This should not happen.")
        return None, None, None, None

    oldest_8 = players[:8]
    player_mmr = [int(p.get('mmr', 1000)) for p in oldest_8]

    best_split = None
    smallest_diff = float('inf')
    indexes = list(range(8))
    for team_a_idxs in combinations(indexes, 4):
        team_b_idxs = [i for i in indexes if i not in team_a_idxs]
        team_a_mmr = sum(player_mmr[i] for i in team_a_idxs)
        team_b_mmr = sum(player_mmr[i] for i in team_b_idxs)
        diff = abs(team_a_mmr - team_b_mmr)
        if diff < smallest_diff:
            smallest_diff = diff
            best_split = (team_a_idxs, team_b_idxs)

    team_a = [oldest_8[i] for i in best_split[0]]
    team_b = [oldest_8[i] for i in best_split[1]]

    if smallest_diff > 50:
        print(f"Warning: Teams are not well balanced. MMR diff: {smallest_diff}")

    game_id = f"Game-{uuid.uuid4().hex[:8]}"

    captain_a_id = None
    captain_b_id = None

    if game_type == "draft_arena":
        team_a_sorted = sorted(team_a, key=lambda p: p.get('mmr', 1000), reverse=True)
        team_b_sorted = sorted(team_b, key=lambda p: p.get('mmr', 1000), reverse=True)
        captain_a_id = team_a_sorted[0]['discord_id']
        captain_b_id = team_b_sorted[0]['discord_id']

    try:
        game_doc_data = {
            '_id': game_id,
            'game_id': game_id,
            'team_a': [],
            'team_b': [],
            'result': 'pending',
            'created_at': datetime.now(timezone.utc),
            'votes': [],
            'game_type': game_type,
        }

        if game_type == "draft_arena":
            game_doc_data.update({
                'captain_a_discord_id': captain_a_id,
                'captain_b_discord_id': captain_b_id,
                'draft_order_type': "Alt",
                'coinflip_winner_team': None,
                'coinflip_choice': None,
                'team_a_picks': [],
                'team_b_picks': [],
                'current_draft_stage': "ready_check", 
                'draft_start_time': datetime.now(timezone.utc),
                'captains_ready': [],
                'draft_message_id': None,
                'current_turn_index': 0,
                'current_turn_captain_id': None,
                'hunters_available': list(HUNTERS),
                'banned_hunters': []
            })
        db.games.insert_one(game_doc_data)

        if game_type == "draft_arena":
            announce_channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
            thread = await announce_channel.create_thread(
                name=f"Draft {game_id}",
                type=discord.ChannelType.public_thread
            )
            db.games.update_one({"_id": game_id}, {
                "$set": {
                    "draft_thread_id": thread.id,
                    "draft_channel_id": thread.id
                }
            })

    except pymongo.errors.DuplicateKeyError:
        print(f"DuplicateKeyError for {game_id} in start_matchmaking. Retrying with new ID.")
        return None, None, None, None

    for player in team_a:
        move_to_ingame(player['discord_id'], game_id, 'team_a')
        remove_from_queue(player['discord_id'])
    for player in team_b:
        move_to_ingame(player['discord_id'], game_id, 'team_b')
        remove_from_queue(player['discord_id'])


    player_to_monitor = team_a[0]
    threading.Thread(target=monitor_game_v2, args=(player_to_monitor.get('ign'), game_id, 'team_a')).start()

    final_game_doc = db.games.find_one({'_id': game_id})
    if final_game_doc:
        return final_game_doc.get('team_a', []), final_game_doc.get('team_b', []), game_id, game_type
    else:
        print(f"Error: Game document {game_id} not found after insertion and updates.")
        return None, None, None, None

def update_game_result(game_id, result):
    db.games.update_one({"_id": game_id}, {"$set": {'result': result}})

    process_match_result(game_id, result)


@tasks.loop(seconds=20)
async def check_and_update_results():
    games = db.games.find({
        "$or": [
            {'result': {'$in': ['team_a', 'team_b', 'canceled', 'timed_out']}},
            {
                'game_type': 'draft_arena',
                'current_draft_stage': {'$in': ['complete', 'draft_complete']},
                'result': {'$nin': ['processed', 'canceled', 'timed_out']}
            }
        ]
    })

    for game in games:
        game_data = game
        game_id = game_data.get('_id')
        result = game_data.get('result')
        game_type = game_data.get('game_type', 'ranked_arena')  

        print(f"Checking result for {game_id} ({game_type}): {result}")

        if result == 'processed':
            print(f"{game_id} has already been processed, skipping.")
            continue

        if result == 'pending':
            draft_stage = game_data.get('current_draft_stage', '').lower()
            if game_type == "draft_arena" and draft_stage in ("complete", "draft_complete"):
                print(f"Draft {game_id} draft complete, awaiting game result.")
            else:
                print(f"The result for {game_id} is currently pending.")
            continue

        if result in ['canceled', 'timed_out']:
            print(f"Game '{game_id}' has been {result}.")

            draft_stage = game_data.get('current_draft_stage', '').lower()
            if game_type == "draft_arena" and draft_stage not in ("complete", "draft_complete"):
                db.games.update_one({'_id': game_id}, {'$set': {'result': 'processed'}})

                if not game_data.get('announced_cancellation'): 
                    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                    if channel:
                        await channel.send(f"{game_id} was automatically canceled due to a {result.replace('_', ' ')}.")
                    db.games.update_one({'_id': game_id}, {'$set': {'announced_cancellation': True}})
            continue

        if result in ['team_a', 'team_b']:
            print(f"{game_id} result found: {result}")

            draft_stage = game_data.get('current_draft_stage', '').lower()
            if game_type == "draft_arena" and draft_stage not in ("complete", "draft_complete"):
                print(f"Draft {game_id} has a result but draft not complete. Skipping MMR processing.")
                continue

            result_str, mmr_changes = process_match_result(game_id, result)

            print("DEBUG mmr_changes:", mmr_changes)

            if result_str and mmr_changes:
                team_a_players = []
                team_b_players = []
                for player in mmr_changes:
                    delta = player['delta']
                    symbol = "+" if delta >= 0 else ""
                    line = f"`{player['ign']}`: {symbol}{delta:.1f} MMR"
                    if player.get("team") == "team_a":
                        team_a_players.append(line)
                    elif player.get("team") == "team_b":
                        team_b_players.append(line)
                    else:
                        print(f"[WARN] No team for {player['ign']}, putting in Team B")
                        team_b_players.append(line)

                embed = discord.Embed(
                    title=f"{'Draft Arena' if game_type == 'draft_arena' else 'Ranked Arena'} Results: {result_str.upper()} Wins!",
                    color=discord.Color.green() if result_str == "team_a" else discord.Color.red()
                )
                embed.add_field(name="Team A", value="\n".join(team_a_players) or "None", inline=False)
                embed.add_field(name="\u200b", value="────────────", inline=False)
                embed.add_field(name="Team B", value="\n".join(team_b_players) or "None", inline=False)
                embed.set_footer(text=f"Game ID: {game_id}")

                if game_type == "draft_arena":
                    thread_id = game_data.get("draft_thread_id")
                    if not thread_id:
                        print(f"[ERROR] No draft_thread_id found for game {game_id}, cannot post results.")
                        continue
                    thread = bot.get_channel(thread_id)
                    if thread is None:
                        try:
                            thread = await bot.fetch_channel(thread_id)
                        except Exception as e:
                            print(f"[ERROR] Could not fetch thread {thread_id} for results: {e}")
                            continue
                    try:
                        await thread.send(embed=embed)
                    except Exception as e:
                        print(f"[ERROR] Failed to send results to thread {thread_id}: {e}")
                else:
                    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                    await channel.send(embed=embed)

                db.games.update_one({'_id': game_id}, {'$set': {'result': 'processed'}})

def format_team_line(team):
    for player in team:
        assert isinstance(player, dict), f"Team member is not a dict: {player!r}"
    return ", ".join([f"<@{player['discord_id']}> ({player.get('ign', 'N/A')})" for player in team])


@tasks.loop(seconds=20)
async def check_queue():
    now = datetime.now(timezone.utc)
    timeout_minutes = 60

    players_in_queue = list(db.in_queue.find({}))
    to_remove = []
    for p in players_in_queue:
        qj = p['queue_joined_at']
        if isinstance(qj, str):
            try:
                qj_dt = datetime.fromisoformat(qj.replace("Z", "+00:00"))
            except Exception:
                qj_dt = datetime.strptime(qj, "%Y-%m-%d %H:%M:%S")
                qj_dt = qj_dt.replace(tzinfo=timezone.utc)
        elif qj.tzinfo is None:
            qj_dt = qj.replace(tzinfo=timezone.utc)
        else:
            qj_dt = qj
        if (now - qj_dt).total_seconds() > timeout_minutes * 60:
            to_remove.append(p)

    kicked_igns = []
    for player in to_remove:
        remove_from_queue(player['discord_id'])
        kicked_igns.append(player.get('ign', 'Unknown Player'))

    if kicked_igns:
        channel = bot.get_channel(ALLOWED_CHANNEL_ID)
        kicked_list = ", ".join(kicked_igns)
        await channel.send(
            f"Removed from the matchmaking queue due to inactivity (60 min limit): {kicked_list}"
        )

    players_by_game_type = {
        "ranked_arena": [],
        "draft_arena": []
    }
    for player in players_in_queue:
        gt = player.get('game_type', 'ranked_arena') 
        if gt in players_by_game_type:
            players_by_game_type[gt].append(player)
        else:
            print(f"[WARN] Unknown game type '{gt}' for player {player.get('ign')}. Skipping.")


    for game_type, current_players_in_queue in players_by_game_type.items():
        
        if len(current_players_in_queue) >= 8:
            
            team_a, team_b, game_id, matched_game_type = await start_matchmaking(current_players_in_queue, bot)

            if team_a and team_b and game_id:
                team_a_line = format_team_line(team_a)
                team_b_line = format_team_line(team_b)

                announce_channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                if announce_channel is None:
                    print(f"[ERROR] Could not find announce channel with ID: {ANNOUNCE_CHANNEL_ID}")
                    return

                if matched_game_type == "draft_arena":
                    game = db.games.find_one({"_id": game_id})
                    thread_id = game["draft_channel_id"]
                    thread = bot.get_channel(thread_id)

                    await thread.send(f"------------------------------------------------------------------------------------------------------------------------------------\n"
                                                f"## Draft Arena Matchmaking successful!\n"
                                                f"Game ID: {game_id}\n"
                                                f"Team A: {team_a_line}\n"
                                                f"Team B: {team_b_line}\n"
                                                f"The draft process will begin publicly here!\n"
                                                f"------------------------------------------------------------------------------------------------------------------------------------")

                    final_game_doc = db.games.find_one({'_id': game_id})
                    if final_game_doc is not None:
                        await prompt_captains_ready(final_game_doc, bot)
                else:
                    await announce_channel.send(f"------------------------------------------------------------------------------------------------------------------------------------\n"
                                                f"## Ranked Arena Matchmaking successful!\n"
                                                f"Game ID: {game_id}\n"
                                                f"Team A: {team_a_line}\n"
                                                f"Team B: {team_b_line}\n"
                                                f"------------------------------------------------------------------------------------------------------------------------------------")

    
@bot.tree.command(name="leaderboard", description="Display the top-ranked players.")
async def leaderboard_command(interaction: discord.Interaction):
    if not check_channel(interaction):
        await interaction.response.send_message("This command can only be used in the specified channel.", ephemeral=True)
        return

    users = list(db.users.find({}).sort("mmr", pymongo.DESCENDING))
    leaderboard_data = users 

    class LeaderboardView(ui.View):
        def __init__(self, data, per_page=10):
            super().__init__(timeout=None)
            self.data = data
            self.per_page = per_page
            self.page = 0
            self.max_page = (len(data) - 1) // per_page

            self.prev_button.disabled = True
            self.next_button.disabled = self.max_page == 0

        async def update_message(self, interaction: discord.Interaction):
            start = self.page * self.per_page
            end = start + self.per_page
            entries = self.data[start:end]

            leaderboard_text = "\n".join([
                f"{start + i + 1}. {user.get('ign', 'N/A')} - {int(user.get('mmr', 0))} MMR"
                for i, user in enumerate(entries)
            ])
            content = f"**Leaderboard** (Page {self.page + 1}/{self.max_page + 1}):\n{leaderboard_text}"
            await interaction.response.edit_message(content=content, view=self)

        @ui.button(label="◀️", style=discord.ButtonStyle.secondary)
        async def prev_button(self, interaction: discord.Interaction, button: ui.Button):
            self.page = max(0, self.page - 1)
            self.prev_button.disabled = self.page == 0
            self.next_button.disabled = self.page == self.max_page
            await self.update_message(interaction)

        @ui.button(label="▶️", style=discord.ButtonStyle.secondary)
        async def next_button(self, interaction: discord.Interaction, button: ui.Button):
            self.page = min(self.max_page, self.page + 1)
            self.prev_button.disabled = self.page == 0
            self.next_button.disabled = self.page == self.max_page
            await self.update_message(interaction)

    view = LeaderboardView(leaderboard_data)
    start = 0
    end = 10
    entries = leaderboard_data[start:end]
    leaderboard_text = "\n".join([
        f"{i + 1}. {user.get('ign', 'N/A')} - {int(user.get('mmr', 0))} MMR"
        for i, user in enumerate(entries)
    ])
    await interaction.response.send_message(
        content=f"**Leaderboard** (Page 1/{view.max_page + 1}):\n{leaderboard_text}",
        view=view,
        ephemeral=True
    )

@bot.tree.command(name="vote_stop", description="Vote to stop/cancel an ongoing game. Needs 6 out of 8 to succeed.")
async def vote_stop_command(interaction: discord.Interaction, game_id: str):
    if not check_channel(interaction):
        await interaction.response.send_message(
            "This command can only be used in the specified channel.", ephemeral=True)
        return


    game_data = db.games.find_one({"_id": game_id})
    if not game_data:
        await interaction.response.send_message(
            f"{game_id} not found.", ephemeral=True)
        return

    if game_data.get('result') in ['canceled', 'processed', 'timed_out', 'team_a', 'team_b']:
        await interaction.response.send_message(
            f"{game_id} is already finished or canceled.", ephemeral=True)
        return

    team_a = game_data.get('team_a', [])
    team_b = game_data.get('team_b', [])
    allowed_voters = {str(p.get('discord_id')) for p in team_a + team_b}

    if str(interaction.user.id) not in allowed_voters:
        await interaction.response.send_message(
            "Only players in this game can vote to cancel it.", ephemeral=True)
        return

    votes = game_data.get('votes', [])
    if interaction.user.id in votes:
        await interaction.response.send_message(
            "You've already voted to stop this game.", ephemeral=True)
        return

    votes.append(interaction.user.id)
    db.games.update_one({"_id": game_id}, {"$set": {"votes": votes}})

    if len(votes) >= 6:
        db.games.update_one({"_id": game_id}, {"$set": {"result": "canceled"}})
        channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
        await channel.send(
            f"{game_id} has been canceled by vote (6/8 or more players agreed). No MMR has been changed."
        )
        await interaction.response.send_message(
            "Your vote was counted and the game is now canceled.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"Your vote was counted. {len(votes)}/8 players have voted to cancel {game_id}. (Need 6 total)",
            ephemeral=True
        )


class MainPanelView(ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.in_queue = self.is_user_in_queue(user_id)
        self.update_button_states(user_id)

    def is_user_in_queue(self, user_id):
        return db.in_queue.find_one({"discord_id": user_id}) is not None

    def update_button_states(self, user_id):
        in_queue = self.is_user_in_queue(user_id)
        if in_queue:
            self.start_ranked_queue.label = "Stop Ranked Queue"
            self.start_ranked_queue.style = discord.ButtonStyle.red
            self.start_draft_queue.label = "Stop Draft Queue"
            self.start_draft_queue.style = discord.ButtonStyle.red
        else:
            self.start_ranked_queue.label = "Start Ranked Queue"
            self.start_ranked_queue.style = discord.ButtonStyle.blurple
            self.start_draft_queue.label = "Start Draft Queue"
            self.start_draft_queue.style = discord.ButtonStyle.red

    @ui.button(label="Start Ranked Queue", style=discord.ButtonStyle.blurple, custom_id="start_ranked_queue_button")
    async def start_ranked_queue(self, interaction: discord.Interaction, button: ui.Button):
        await self._handle_queue_button(interaction, "ranked_arena", button)

    @ui.button(label="Start Draft Queue", style=discord.ButtonStyle.red, custom_id="start_draft_queue_button")
    async def start_draft_queue(self, interaction: discord.Interaction, button: ui.Button):
        await self._handle_queue_button(interaction, "draft_arena", button)


    @ui.button(label="Create/Edit IGN", style=discord.ButtonStyle.green)
    async def create_edit_ign(self, interaction: discord.Interaction, button: ui.Button):
        class EditIgnModal(ui.Modal, title="Set or Edit your IGN"):
            ign_tag = ui.TextInput(label="Enter your IGN#TAG", required=True, max_length=32)
            async def on_submit(modal_self, interaction2: discord.Interaction):
                user_id = interaction2.user.id
                user_data = db.users.find_one({"discord_id": user_id})

                if not user_data:
                    db.users.insert_one({
                        'discord_id': user_id,
                        'ign': str(modal_self.ign_tag),
                        'mmr': 1000,
                        'confidence': 300,
                        'games_played': 0,
                        'wins': 0,
                        'losses': 0
                    })
                else:
                    db.users.update_one(
                        {"discord_id": user_id},
                        {"$set": {'ign': str(modal_self.ign_tag)}}
                    )

                embed = discord.Embed(title="✅ IGN Updated!", color=discord.Color.green())
                embed.description = f"Your IGN is now: **{modal_self.ign_tag}**"
                await interaction2.response.edit_message(
                    content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
                    embed=embed,
                    view=self
                )
        await interaction.response.send_modal(EditIgnModal())

    @ui.button(label="Check Queue", style=discord.ButtonStyle.gray)
    async def check_queue(self, interaction: discord.Interaction, button: ui.Button):

        ranked_players = list(db.in_queue.find({"game_type": "ranked_arena"}).sort("queue_joined_at", pymongo.ASCENDING))
        draft_players = list(db.in_queue.find({"game_type": "draft_arena"}).sort("queue_joined_at", pymongo.ASCENDING))

        embed = discord.Embed(title="Queue Status", color=discord.Color.blue())


        if not ranked_players:
            ranked_desc = "No players are currently in the Ranked Arena queue."
        else:
            ranked_list = []
            for player_data in ranked_players:
                ign = player_data.get('ign', 'No IGN')
                mmr = int(round(player_data.get('mmr', 1000)))
                ranked_list.append(f"{ign} - {mmr} MMR")
            ranked_desc = "\n".join(ranked_list)
        embed.add_field(name=f"Ranked Arena Queue ({len(ranked_players)}/8)", value=ranked_desc, inline=False)


        embed.add_field(name="\u200b", value="\u200b", inline=False)


        if not draft_players:
            draft_desc = "No players are currently in the Draft Arena queue."
        else:
            draft_list = []
            for player_data in draft_players:
                ign = player_data.get('ign', 'No IGN')
                mmr = int(round(player_data.get('mmr', 1000)))
                draft_list.append(f"{ign} - {mmr} MMR")
            draft_desc = "\n".join(draft_list)
        embed.add_field(name=f"Draft Arena Queue ({len(draft_players)}/8)", value=draft_desc, inline=False)

        embed.set_footer(text="Updated automatically every 30 seconds.")

        await interaction.response.edit_message(
            content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
            embed=embed,
            view=self
        )

    @ui.button(label="My Data", style=discord.ButtonStyle.blurple)
    async def my_data(self, interaction: discord.Interaction, button: ui.Button):
        user_data = get_user_data(interaction.user.id)
        if user_data:
            embed = discord.Embed(title="📊 Your Data", color=discord.Color.blue())
            embed.add_field(name="IGN", value=user_data['ign'], inline=False)
            embed.add_field(name="MMR", value=str(user_data['mmr']), inline=True)
            embed.add_field(name="Wins", value=str(user_data['wins']), inline=True)
            embed.add_field(name="Losses", value=str(user_data['losses']), inline=True)
            embed.add_field(name="Games Played", value=str(user_data['games_played']), inline=True)
        else:
            embed = discord.Embed(title="Not Found", description="No user data found.", color=discord.Color.red())
        await interaction.response.edit_message(
            content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
            embed=embed,
            view=self
        )


    @ui.button(label="User Data", style=discord.ButtonStyle.gray)
    async def user_data(self, interaction: discord.Interaction, button: ui.Button):
        class UserDataModal(ui.Modal, title="Check User Data"):
            search_ign = ui.TextInput(label="Enter IGN#TAG to lookup", required=True, max_length=32)
            async def on_submit(modal_self, interaction2: discord.Interaction):
                try:
                    other_user = get_user_data_by_ign(str(modal_self.search_ign))
                    if other_user:
                        embed = discord.Embed(title=f"User Data for {modal_self.search_ign}", color=discord.Color.purple())
                        embed.add_field(name="MMR", value=str(other_user['mmr']), inline=True)
                        embed.add_field(name="Wins", value=str(other_user['wins']), inline=True)
                        embed.add_field(name="Losses", value=str(other_user['losses']), inline=True)
                        embed.add_field(name="Games Played", value=str(other_user['games_played']), inline=True)
                    else:
                        embed = discord.Embed(title="Not Found", description="No data found for that IGN.", color=discord.Color.red())
                    await interaction2.response.edit_message(
                        content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
                        embed=embed,
                        view=self
                    )
                except Exception as e:
                    embed = discord.Embed(title="Error", description=f"Something went wrong. Try again.\n{e}", color=discord.Color.red())
                    await interaction2.response.edit_message(
                        content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
                        embed=embed,
                        view=self
                    )
        await interaction.response.send_modal(UserDataModal())

    @ui.button(label="Leaderboard", style=discord.ButtonStyle.gray)
    async def leaderboard(self, interaction: discord.Interaction, button: ui.Button):
        users = list(db.users.find({}))
        leaderboard_data = sorted(users, key=lambda x: x.get('mmr', 0), reverse=True)
        page = 0

        view = LeaderboardPanelView(leaderboard_data, page=page, panel_view=self)
        embed = view.get_embed()
        await interaction.response.edit_message(embed=embed, view=view)


    @ui.button(label="Vote Stop", style=discord.ButtonStyle.danger)
    async def vote_stop(self, interaction: discord.Interaction, button: ui.Button):
        class VoteModal(ui.Modal, title="Vote to Stop Game"):
            game_id = ui.TextInput(label="Enter Game ID", required=True, max_length=20)
            async def on_submit(modal_self, interaction2: discord.Interaction):
                success, msg = process_vote_stop(interaction2.user.id, str(modal_self.game_id))
                if success:
                    embed = discord.Embed(title="Vote Stop", description=msg, color=discord.Color.green())
                    if "has been canceled by vote" in msg:
                        channel = bot.get_channel(ALLOWED_CHANNEL_ID)
                        await channel.send(msg)
                else:
                    embed = discord.Embed(title="Vote Stop Error", description=msg, color=discord.Color.red())
                await interaction2.response.edit_message(
                    content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
                    embed=embed,
                    view=self
                )
        await interaction.response.send_modal(VoteModal())


    async def _handle_queue_button(self, interaction: discord.Interaction, game_type: str, button: ui.Button):
        user_id = interaction.user.id
        user_profile = get_user_data(user_id)
        if not user_profile:
            embed = discord.Embed(title="Error", description="You need to create a user profile first using 'Create/Edit IGN'.", color=discord.Color.red())
            await interaction.response.edit_message(
                content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
                embed=embed, view=self
            )
            return

        current_queue_doc = db.in_queue.find_one({"discord_id": user_id})
        in_game = is_user_in_ongoing_game(user_id)
        embed = discord.Embed()

        if in_game:
            embed.title = "Error"
            embed.description = "You are already in an ongoing game. Wait for it to finish before queueing again."
            embed.color = discord.Color.red()
        elif current_queue_doc:
            if current_queue_doc['game_type'] == game_type:

                if remove_from_queue(user_id):
                    embed.title = f"❌ You left the {game_type.replace('_', ' ').title()} queue!"
                    embed.description = f"You're no longer waiting for a {game_type.replace('_', ' ')} game."

                    self.start_ranked_queue.label = "Start Ranked Queue"
                    self.start_ranked_queue.style = discord.ButtonStyle.blurple
                    self.start_draft_queue.label = "Start Draft Queue"
                    self.start_draft_queue.style = discord.ButtonStyle.blurple
                else:
                    embed.title = "Error"
                    embed.description = "You weren't in the queue."
                    embed.color = discord.Color.red()
            else:

                embed.title = "Error"
                embed.description = f"You are already in the **{current_queue_doc['game_type'].replace('_', ' ').title()}** queue. Please leave it first before joining another."
                embed.color = discord.Color.red()
        else:

            success, reason = add_to_queue(user_id, game_type)
            if success:
                embed.title = f"🚦 You joined the {game_type.replace('_', ' ').title()} queue!"
                embed.description = f"You're now waiting for a {game_type.replace('_', ' ')} game."
                if game_type == "ranked_arena":
                    button.label = "Stop Ranked Queue"
                    button.style = discord.ButtonStyle.red
                    self.start_draft_queue.disabled = True
                elif game_type == "draft_arena":
                    button.label = "Stop Draft Queue"
                    button.style = discord.ButtonStyle.red
                    self.start_ranked_queue.disabled = True
            else:
                embed.title = "Error"
                if reason == "no_profile":
                    embed.description = "You need to create a user profile first."
                elif reason == "in_game":
                    embed.description = "You are already in an active game."
                else:
                    embed.description = "Failed to join queue for an unknown reason."
                embed.color = discord.Color.red()

        self.update_button_states(user_id)

        await interaction.response.edit_message(
            content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
            embed=embed, view=self
        )

    def is_user_in_ongoing_game(user_id):
        ongoing_games = list(db.games.find({
            "$or": [
                {'result': {'$in': ['pending', 'team_a', 'team_b']}}, 
                { 
                    'game_type': "draft_arena",
                    'result': {'$ne': 'processed'}, 
                    'current_draft_stage': {'$nin': ["complete", "timed_out_draft_ready_check", "timed_out_draft_coinflip", "timed_out_draft_turn"]}
                }
            ]
        }))
        for game in ongoing_games:
            team_a = game.get('team_a', [])
            team_b = game.get('team_b', [])
            ids_in_a = {str(p.get('discord_id')) for p in team_a}
            ids_in_b = {str(p.get('discord_id')) for p in team_b}
            if str(user_id) in ids_in_a or str(user_id) in ids_in_b:
                return True
        return False

    def update_button_states(self, user_id):
        current_queue_doc = db.in_queue.find_one({"discord_id": user_id})
        in_game = is_user_in_ongoing_game(user_id)


        self.start_ranked_queue.disabled = False
        self.start_draft_queue.disabled = False
        self.start_ranked_queue.label = "Start Ranked Queue"
        self.start_ranked_queue.style = discord.ButtonStyle.blurple
        self.start_draft_queue.label = "Start Draft Queue"
        self.start_draft_queue.style = discord.ButtonStyle.red

        if in_game:
            self.start_ranked_queue.disabled = True
            self.start_draft_queue.disabled = True
        elif current_queue_doc:
            if current_queue_doc['game_type'] == "ranked_arena":
                self.start_ranked_queue.label = "Stop Ranked Queue"
                self.start_ranked_queue.style = discord.ButtonStyle.red
                self.start_draft_queue.disabled = True
            elif current_queue_doc['game_type'] == "draft_arena":
                self.start_draft_queue.label = "Stop Draft Queue"
                self.start_draft_queue.style = discord.ButtonStyle.green 
                self.start_ranked_queue.disabled = True


class AccessUIButton(ui.View):
    @ui.button(label="Access UI", style=discord.ButtonStyle.green)
    async def access_ui(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
            view=MainPanelView(interaction.user.id),  
            ephemeral=True
        )


def get_current_draft_turn(game_doc, draft_orders):
    draft_order_type = game_doc.get('draft_order_type', 'Alt')
    current_order = draft_orders.get(draft_order_type)
    if not current_order:
        return None, None 

    current_pick_number = len(game_doc.get('draft_picks', []))
    if current_pick_number >= len(current_order):
        return "finished", None

    turn_info = current_order[current_pick_number]
    team_role = turn_info['team_role']
    action = turn_info['action']
    count = turn_info['count']

    captain_id = None
    if team_role == "captain_a":
        captain_id = game_doc['captain_a_discord_id']
    elif team_role == "captain_b":
        captain_id = game_doc['captain_b_discord_id']

    return captain_id, action, count

class ReadyCheckView(discord.ui.View):
    def __init__(self, game_id, captain_id, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.captain_id = captain_id
        self.bot = bot

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.success)
    async def ready_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.captain_id:
            await interaction.response.send_message("You are not this captain!", ephemeral=True)
            return


        button.disabled = True
        await interaction.response.edit_message(view=self)


        db.games.update_one({"_id": self.game_id}, {"$addToSet": {"captains_ready": self.captain_id}})
        await interaction.followup.send("You are marked as ready! Waiting for the other captain...", ephemeral=True)


        game = db.games.find_one({"_id": self.game_id})

        if len(game.get("captains_ready", [])) == 2 and game.get("current_draft_stage") == "ready_check":
            thread = bot.get_channel(game["draft_channel_id"])
            await thread.send("Both captains are ready! Time for the coinflip.")
            await start_coinflip_phase(game, self.bot)

    async def on_timeout(game, self):
        game = db.games.find_one({"_id": self.game_id})
        game_doc = db.games.find_one({"_id": self.game_id})
        if game_doc and game_doc.get('current_draft_stage') == "ready_check":
            db.games.update_one({"_id": self.game_id}, {"$set": {"result": "timed_out_draft_ready_check"}})
            thread = bot.get_channel(game["draft_channel_id"])
            if thread:
                await thread.send(f"Draft game {self.game_id} timed out during ready check. Game canceled.")

async def start_coinflip_phase(game, bot):
    captain_to_choose = game["captain_a_discord_id"]
    thread = bot.get_channel(game["draft_channel_id"])
    await thread.send(
        f"<@{captain_to_choose}>, please choose Heads or Tails to determine who drafts first!",
        view=CoinflipView(game["_id"], captain_to_choose, bot)
    )
    db.games.update_one({"_id": game["_id"]}, {"$set": {"current_draft_stage": "coinflip"}})

def get_other_captain(game_id, captain_id):
    game = db.games.find_one({"_id": game_id})
    a = game["captain_a_discord_id"]
    b = game["captain_b_discord_id"]
    return b if captain_id == a else a

async def prompt_captains_ready(game, bot):
    thread = bot.get_channel(game["draft_channel_id"])
    for captain_id in [game["captain_a_discord_id"], game["captain_b_discord_id"]]:
        await thread.send(
            f"<@{captain_id}>, please click Ready below to start the draft!",
            view=ReadyCheckView(game["_id"], captain_id, bot)
        )
    
class CoinflipView(discord.ui.View):
    def __init__(self, game_id, captain_id, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.captain_id = captain_id
        self.bot = bot

    @discord.ui.button(label="Heads", style=discord.ButtonStyle.primary)
    async def heads_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "heads")

    @discord.ui.button(label="Tails", style=discord.ButtonStyle.primary)
    async def tails_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "tails")

    async def handle_choice(self, interaction, choice):
        if interaction.user.id != self.captain_id:
            await interaction.response.send_message("You are not the coinflip captain!", ephemeral=True)
            return

        import random
        flip_result = random.choice(["heads", "tails"])
        win = (choice == flip_result)

        game = db.games.find_one({"_id": self.game_id})
        captain_a = game["captain_a_discord_id"]
        captain_b = game["captain_b_discord_id"]
        winner_captain_id = self.captain_id if win else (captain_b if self.captain_id == captain_a else captain_a)
        first_action_type = "ban"

        db.games.update_one(
            {"_id": self.game_id},
            {"$set": {
                "coinflip_choice": choice,
                "coinflip_result": flip_result,
                "coinflip_winner_team": "team_a" if win else "team_b",
                "current_draft_stage": "draft_in_progress",
                "current_turn_captain_id": winner_captain_id,
                "current_action_type": first_action_type
            }}
        )

        await interaction.response.send_message(
            f"You chose {choice}. Coinflip result: {flip_result}. "
            f"{'You win the coinflip!' if win else 'Opponent wins the coinflip!'}",
            ephemeral=True
        )

        game = db.games.find_one({"_id": self.game_id})
        thread = self.bot.get_channel(game["draft_channel_id"])
        await thread.send(
            f"Coinflip! <@{self.captain_id}> chose **{choice}**. The coin landed on **{flip_result}**.\n"
            f"<@{winner_captain_id}> will start the draft!"
        )

        updated_game = db.games.find_one({"_id": self.game_id})
        available_hunters = updated_game["hunters_available"]
        msg = await thread.send(
            f"<@{winner_captain_id}>, it's your turn to {first_action_type}:",
            view=DraftActionView(self.game_id, available_hunters, winner_captain_id, first_action_type, self.bot, ephemeral_tracker)
        )
        db.games.update_one({"_id": self.game_id}, {"$set": {"draft_action_msg_id": msg.id}})

        await update_draft_message(self.game_id, thread, self.bot)

    async def on_timeout(game, self):
        game = db.games.find_one({"_id": self.game_id})
        game_doc = db.games.find_one({"_id": self.game_id})
        if game_doc and game_doc.get('current_draft_stage') == "coinflip":
            db.games.update_one({"_id": self.game_id}, {"$set": {"result": "timed_out_draft_coinflip"}})
            thread = self.bot.get_channel(game["draft_channel_id"])
            if thread:
                await thread.send(f"Draft game {self.game_id} timed out during coinflip. Game canceled.")


async def update_draft_message(game_id, thread, bot):
    game = db.games.find_one({"_id": game_id})

    is_complete = game.get("current_draft_stage", "").lower() in ["complete", "completed"]


    current_captain_id = game.get("current_turn_captain_id")
    if is_complete:
        turn_text = "Draft finished!"
        thread = bot.get_channel(game["draft_thread_id"])
    elif current_captain_id:
        turn_text = f"<@{current_captain_id}>'s turn"
    else:
        turn_text = "Waiting for next action..."


    phase_lookup = {
        "coinflip": "Coinflip (waiting for captain to choose)",
        "ban_phase_initial": "Initial Ban Phase",
        "pick_phase": "Pick Phase",
        "draft_in_progress": "Draft In Progress",
        "complete": "Completed",
        "completed": "Completed"
    }
    phase = game.get("current_draft_stage", "Unknown")
    phase_text = phase_lookup.get(phase, phase)


    team_a_picks = game.get("team_a_picks", [])
    team_b_picks = game.get("team_b_picks", [])
    banned_hunters = game.get("banned_hunters", [])
    available_hunters = game.get("hunters_available", [])


    coinflip_str = ""
    if phase == "coinflip":
        captain_a = game.get("captain_a_discord_id")
        captain_b = game.get("captain_b_discord_id")
        coinflip_str = (
            f"Coinflip in progress!\n"
            f"{f'<@{captain_a}>' if captain_a else 'Captain A'} vs {f'<@{captain_b}>' if captain_b else 'Captain B'}"
        )
        if game.get("coinflip_choice"):
            coinflip_str += f"\nChosen: **{game['coinflip_choice'].capitalize()}**"
        if game.get("coinflip_result"):
            coinflip_str += f"\nResult: **{game['coinflip_result'].capitalize()}**"
            winner = game.get("coinflip_winner_team")
            if winner:
                winner_captain = captain_a if winner == "team_a" else captain_b
                coinflip_str += f"\nWinner: <@{winner_captain}>"
                

    game = db.games.find_one({"_id": game_id})

    embed = discord.Embed(
        title=f"Draft Status: {game.get('game_id', game_id)}",
        description=f"**Phase:** {phase_text}\n**Turn:** {turn_text}"
    )
    if coinflip_str:
        embed.add_field(name="Coinflip", value=coinflip_str, inline=False)
    embed.add_field(name="Team A Picks", value=", ".join(team_a_picks) or "None", inline=True)
    embed.add_field(name="Team B Picks", value=", ".join(team_b_picks) or "None", inline=True)
    embed.add_field(name="Banned", value=", ".join(banned_hunters) or "None", inline=False)
    embed.add_field(name="Available", value=", ".join(available_hunters) or "None", inline=False)


    if game.get("last_action"):
        embed.set_footer(text=f"Last action: {game['last_action']}")


    draft_msg_id = game.get("draft_message_id")
    msg = None
    if draft_msg_id:
        try:
            msg = await thread.fetch_message(draft_msg_id)
            await msg.edit(embed=embed)
        except discord.NotFound:

            msg = await thread.send(embed=embed)
            db.games.update_one({"_id": game_id}, {"$set": {"draft_message_id": msg.id}})
    else:
        msg = await thread.send(embed=embed)
        db.games.update_one({"_id": game_id}, {"$set": {"draft_message_id": msg.id}})


class DraftActionView(discord.ui.View):
    def __init__(self, game_id, available, captain_id, action_type, bot, ephemeral_tracker):
        super().__init__(timeout=None)
        self.add_item(DraftActionSelect(game_id, available, captain_id, action_type, bot, ephemeral_tracker))

def get_next_turn_and_phase(game, last_action_type):

    a_id = game["captain_a_discord_id"]
    b_id = game["captain_b_discord_id"]
    coinflip_winner_team = game.get("coinflip_winner_team", "team_a")
    if coinflip_winner_team == "team_a":
        first = a_id
        second = b_id
    else:
        first = b_id
        second = a_id


    draft_turns = [
        (first, "ban"),  
        (second, "ban"),   
        (first, "pick"),  
        (second, "pick"),  
        (second, "pick"),  
        (first, "pick"),   
        (second, "pick"), 
        (first, "pick"),   
        (first, "pick"),  
        (second, "pick"),  
    ]

    turn_index = game.get("current_turn_index", 0) + 1

    if turn_index >= len(draft_turns):
        return None, "complete" 

    next_captain_id, next_action_type = draft_turns[turn_index]

    db.games.update_one({"_id": game["_id"]}, {"$set": {"current_turn_index": turn_index}})
    return next_captain_id, next_action_type



class DraftActionSelect(discord.ui.Select):
    def __init__(self, game_id, available, captain_id, action_type, bot, ephemeral_tracker):
        options = [discord.SelectOption(label=char) for char in available]
        super().__init__(
            placeholder=f"Choose a character to {action_type}...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.game_id = game_id
        self.captain_id = captain_id
        self.action_type = action_type 
        self.bot = bot
        self.ephemeral_tracker = ephemeral_tracker

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.captain_id:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return

        selected = self.values[0]
        game = db.games.find_one({"_id": self.game_id})


        updates = {}
        if self.action_type == "ban":
            updates["$addToSet"] = {"banned_hunters": selected}
            updates["$pull"] = {"hunters_available": selected}
        elif self.action_type == "pick":
            team_key = "team_a_picks" if interaction.user.id == game["captain_a_discord_id"] else "team_b_picks"
            updates["$addToSet"] = {team_key: selected}
            updates["$pull"] = {"hunters_available": selected}
        db.games.update_one({"_id": self.game_id}, updates)


        game = db.games.find_one({"_id": self.game_id}) 
        next_captain_id, next_action_type = get_next_turn_and_phase(game, self.action_type)
        db.games.update_one(
            {"_id": self.game_id},
            {"$set": {"current_turn_captain_id": next_captain_id, "current_action_type": next_action_type}}
        )

        key = (self.game_id, interaction.user.id)


        if self.action_type == "ban" and key not in self.ephemeral_tracker:

            await interaction.response.send_message(
                f"You banned {selected}!",
                ephemeral=True
            )
            self.ephemeral_tracker[key] = interaction  
        elif key in self.ephemeral_tracker:

            prev_interaction = self.ephemeral_tracker[key]
            try:
                await prev_interaction.edit_original_response(
                    content=f"You picked {selected}!"
                )
            except Exception as e:
                print(f"Failed to edit ephemeral message: {e}")
            await interaction.response.defer()  
        else:

            await interaction.response.send_message(
                f"You picked {selected}!",
                ephemeral=True
            )
            self.ephemeral_tracker[key] = interaction



        thread = self.bot.get_channel(game["draft_channel_id"])
        msg = await thread.fetch_message(game["draft_action_msg_id"])
        game = db.games.find_one({"_id": self.game_id})  

        if next_action_type == "complete":
            db.games.update_one({"_id": self.game_id}, {"$set": {"current_draft_stage": "complete"}})
            await asyncio.sleep(0.05)
            await update_draft_message(self.game_id, thread, self.bot)
            await msg.edit(content="Draft complete!", view=None)
            game = db.games.find_one({"_id": self.game_id})
        else:

            next_available = game["hunters_available"]
            await msg.edit(
                content=f"<@{next_captain_id}>, it's your turn to {next_action_type}:",
                view=DraftActionView(self.game_id, next_available, next_captain_id, next_action_type, self.bot, self.ephemeral_tracker)
            )
            await update_draft_message(self.game_id, thread, self.bot)

@tasks.loop(minutes=10) 
async def cleanup_old_draft_threads():
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    
    old_games = db.games.find({
        "draft_start_time": {"$lt": one_hour_ago}
    })
    
    for game in old_games:
        thread_id = game.get("draft_thread_id")
        if not thread_id:
            print(f"[WARN] No draft_thread_id for game {game.get('_id')}, skipping deletion.")
            continue
        thread = bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await bot.fetch_channel(thread_id)
            except Exception as e:
                print(f"[ERROR] Could not fetch thread {thread_id}: {e}")
                continue

        if isinstance(thread, Thread):
            try:
                await thread.delete()
                print(f"Deleted draft thread {thread_id}")
                db.games.update_one({"_id": game["_id"]}, {"$set": {"thread_deleted": True}})
            except Exception as e:
                print(f"[ERROR] Failed to delete thread {thread_id}: {e}")
        else:
            print(f"[WARN] Object with id {thread_id} is not a thread, skipping deletion.")


async def post_access_ui_message(channel, embed=None):
    global last_access_ui_message

    async for msg in channel.history(limit=20):
        if (
            msg.author == channel.guild.me
            and msg.content.startswith("Press the button below to access your personal Arena Panel!")
        ):
            try:
                await msg.delete()
            except Exception:
                pass

    msg = await channel.send(
        "Press the button below to access your personal Arena Panel!",
        embed=embed,
        view=AccessUIButton()
    )
    last_access_ui_message = msg

def get_queue_status_embed():
    ranked_players = list(db.in_queue.find({"game_type": "ranked_arena"}).sort("queue_joined_at", pymongo.ASCENDING))
    draft_players = list(db.in_queue.find({"game_type": "draft_arena"}).sort("queue_joined_at", pymongo.ASCENDING))

    embed = discord.Embed(title="Queue Status", color=discord.Color.blue())


    if not ranked_players:
        ranked_desc = "No players are currently in the Ranked Arena queue."
    else:
        ranked_list = []
        for player_data in ranked_players:
            ign = player_data.get('ign', 'No IGN')
            mmr = int(round(player_data.get('mmr', 1000)))
            ranked_list.append(f"{ign} - {mmr} MMR")
        ranked_desc = "\n".join(ranked_list)
    embed.add_field(name=f"Ranked Arena Queue ({len(ranked_players)}/8)", value=ranked_desc, inline=False)


    embed.add_field(name="\u200b", value="\u200b", inline=False) 
    if not draft_players:
        draft_desc = "No players are currently in the Draft Arena queue."
    else:
        draft_list = []
        for player_data in draft_players:
            ign = player_data.get('ign', 'No IGN')
            mmr = int(round(player_data.get('mmr', 1000)))
            draft_list.append(f"{ign} - {mmr} MMR")
        draft_desc = "\n".join(draft_list)
    embed.add_field(name=f"Draft Arena Queue ({len(draft_players)}/8)", value=draft_desc, inline=False)

    embed.set_footer(text="Updated automatically every 30 seconds.")
    return embed

@tasks.loop(seconds=30)
async def update_access_ui_embed():
    global last_access_ui_message
    if last_access_ui_message:
        try:
            embed = get_queue_status_embed()
            await last_access_ui_message.edit(embed=embed)
        except Exception as e:

            pass

def is_user_in_ongoing_game(user_id):

    ongoing_games = list(db.games.find({'result': {'$in': ['pending', 'team_a', 'team_b']}}))
    for game in ongoing_games: 
        team_a = game.get('team_a', [])
        team_b = game.get('team_b', [])
        ids_in_a = {str(p.get('discord_id')) for p in team_a}
        ids_in_b = {str(p.get('discord_id')) for p in team_b}
        if str(user_id) in ids_in_a or str(user_id) in ids_in_b:
            return True
    return False


@tasks.loop(minutes=3)
async def refresh_access_ui_message():
    channel = bot.get_channel(ALLOWED_CHANNEL_ID)
    embed = get_queue_status_embed()
    await post_access_ui_message(channel, embed=embed)

class LeaderboardPanelView(discord.ui.View):
    def __init__(self, data, page=0, panel_view=None):
        super().__init__(timeout=None)
        self.data = data
        self.page = page
        self.per_page = 10
        self.max_page = (len(data) - 1) // self.per_page
        self.panel_view = panel_view

    def get_embed(self):
        start = self.page * self.per_page
        end = start + self.per_page
        entries = self.data[start:end]
        leaderboard_text = "\n".join([
            f"{start + i + 1}. {user.get('ign', 'N/A')} - {int(user.get('mmr', 0))} MMR"
            for i, user in enumerate(entries)
        ])
        embed = discord.Embed(
            title=f"Leaderboard (Page {self.page + 1}/{self.max_page + 1}):",
            description=leaderboard_text,
            color=discord.Color.gold()
        )
        return embed

    async def update_leaderboard(self, interaction):
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_leaderboard(interaction)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.max_page:
            self.page += 1
            await self.update_leaderboard(interaction)

    @discord.ui.button(label="Back to Panel", style=discord.ButtonStyle.gray)
    async def back_to_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.panel_view:
            await interaction.response.edit_message(
                content="**Arena Panel:**\nUse the buttons below! Only you will see your info as a popup.",
                embed=None,
                view=self.panel_view
            )   

bot.run(config["bot_token"])
