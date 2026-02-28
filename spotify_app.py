"""
Spotify Playlist Prompt Builder + Playlist Creator
====================================================
Two Modes:
  1. PROMPT BUILDER — AI helps write a text prompt for Spotify's AI playlist feature
  2. PLAYLIST CREATOR — AI searches your library, picks matching songs, creates a real playlist

Architecture:
- Flask handles web routes and session management
- Spotipy handles all Spotify API calls
- OpenAI (or compatible) API powers the chatbot
- Library data is fetched once: summarized for prompt mode, full list for creator mode
"""

import os
import json
import logging
from collections import Counter, defaultdict
from flask import Flask, session, request, redirect, url_for, render_template, jsonify
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(64))

# Server-side caches (too large for session cookies)
# Key: Spotify user ID
library_cache = {}       # summary string (for prompt builder mode)
full_library_cache = {}  # full song list with IDs (for playlist creator mode)

# Spotify credentials
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')

# Scopes — playlist-modify-public AND playlist-modify-private for creating playlists
SCOPE = 'user-library-read playlist-read-private user-top-read playlist-modify-public playlist-modify-private'

# LLM settings
LLM_API_KEY = os.environ.get('OPENAI_API_KEY', '')
LLM_MODEL = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')

if not CLIENT_ID or not CLIENT_SECRET:
    logger.warning("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET not set!")
if not LLM_API_KEY:
    logger.warning("OPENAI_API_KEY not set — chatbot will not work.")


# ---------------------------------------------------------------------------
# Spotify helpers
# ---------------------------------------------------------------------------

def get_spotify():
    """Create a per-request Spotify client tied to the current user's session."""
    cache_handler = FlaskSessionCacheHandler(session)
    sp_oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=cache_handler,
        show_dialog=True
    )
    sp = Spotify(auth_manager=sp_oauth)
    return sp, sp_oauth, cache_handler


def get_access_token():
    """
    Get the current valid access token string from the session.
    This is needed for direct API calls that bypass Spotipy
    (because Spotipy's methods hit deprecated endpoints).
    """
    cache_handler = FlaskSessionCacheHandler(session)
    sp_oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=cache_handler,
    )
    token_info = cache_handler.get_cached_token()
    # If token is expired, refresh it
    if sp_oauth.is_token_expired(token_info):
        token_info = sp_oauth.refresh_access_token(token_info['refresh_token'])
        cache_handler.save_token_to_cache(token_info)
    return token_info['access_token']


def is_authenticated():
    """Check if the current user has a valid Spotify token."""
    cache_handler = FlaskSessionCacheHandler(session)
    sp_oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=cache_handler,
    )
    token = cache_handler.get_cached_token()
    return sp_oauth.validate_token(token) is not None


# ---------------------------------------------------------------------------
# Library Fetching — NO LIMITS (app serves <5 people)
# ---------------------------------------------------------------------------

def fetch_all_liked_songs(sp):
    """
    Fetch ALL liked songs. No cap — we need every song so the AI
    can search the complete library when building playlists.
    Returns list of dicts with 'id', 'uri', 'name', 'artist', 'album'.
    """
    songs = []
    try:
        results = sp.current_user_saved_tracks(limit=50)
        while results:
            for item in results.get('items', []):
                track = item.get('track')
                if not track or not track.get('id'):
                    continue
                songs.append({
                    'id': track['id'],
                    'uri': track['uri'],          # needed to add to playlist
                    'name': track['name'],
                    'artist': track['artists'][0]['name'] if track.get('artists') else 'Unknown',
                    'album': track['album']['name'] if track.get('album') else 'Unknown',
                    'source': 'liked',
                })
            if results.get('next'):
                results = sp.next(results)
            else:
                break
    except Exception as e:
        logger.error(f"Error fetching liked songs: {e}")
    logger.info(f"Fetched {len(songs)} liked songs (all)")
    return songs


def fetch_all_playlist_songs(sp):
    """
    Fetch ALL songs from ALL playlists. Paginates through everything.
    Returns (all_songs_list, playlists_metadata_list).
    """
    all_songs = []
    playlists_meta = []

    try:
        results = sp.current_user_playlists(limit=50)
        while results:
            for item in results.get('items', []):
                if not item:
                    continue
                playlist_id = item.get('id')
                playlist_name = item.get('name', 'Untitled')
                track_count = item.get('tracks', {}).get('total', 0)

                playlists_meta.append({
                    'name': playlist_name,
                    'tracks': track_count,
                })

                # Fetch ALL songs from this playlist
                if playlist_id and track_count > 0:
                    try:
                        tracks_result = sp.playlist_tracks(
                            playlist_id,
                            fields='items(track(id,uri,name,artists(name),album(name))),next'
                        )
                        while tracks_result:
                            for t_item in tracks_result.get('items', []):
                                track = t_item.get('track')
                                if not track or not track.get('id'):
                                    continue
                                all_songs.append({
                                    'id': track['id'],
                                    'uri': track['uri'],
                                    'name': track['name'],
                                    'artist': track['artists'][0]['name'] if track.get('artists') else 'Unknown',
                                    'album': track['album']['name'] if track.get('album') else 'Unknown',
                                    'source': f'playlist:{playlist_name}',
                                })
                            if tracks_result.get('next'):
                                tracks_result = sp.next(tracks_result)
                            else:
                                break
                    except Exception as e:
                        logger.error(f"Error fetching tracks for playlist '{playlist_name}': {e}")

            if results.get('next'):
                results = sp.next(results)
            else:
                break
    except Exception as e:
        logger.error(f"Error fetching playlists: {e}")

    logger.info(f"Fetched {len(all_songs)} songs from {len(playlists_meta)} playlists")
    return all_songs, playlists_meta


def fetch_top_artists(sp):
    """Fetch top artists across all time ranges."""
    artists = {}
    for time_range in ['short_term', 'medium_term', 'long_term']:
        try:
            results = sp.current_user_top_artists(limit=50, time_range=time_range)
            for i, artist in enumerate(results.get('items', [])):
                aid = artist.get('id')
                if not aid or aid in artists:
                    continue
                artists[aid] = {
                    'name': artist.get('name', 'Unknown'),
                    'genres': artist.get('genres', []),
                    'popularity': artist.get('popularity', 0),
                    'rank': i,
                    'time_range': time_range,
                }
        except Exception as e:
            logger.error(f"Error fetching top artists ({time_range}): {e}")
    logger.info(f"Fetched {len(artists)} unique top artists")
    return list(artists.values())


def fetch_top_tracks(sp):
    """Fetch top tracks (medium term)."""
    tracks = []
    try:
        results = sp.current_user_top_tracks(limit=50, time_range='medium_term')
        for track in results.get('items', []):
            if not track or not track.get('id'):
                continue
            tracks.append({
                'name': track['name'],
                'artist': track['artists'][0]['name'] if track.get('artists') else 'Unknown',
            })
    except Exception as e:
        logger.error(f"Error fetching top tracks: {e}")
    logger.info(f"Fetched {len(tracks)} top tracks")
    return tracks


def deduplicate_songs(liked_songs, playlist_songs):
    """
    Merge liked + playlist songs, removing duplicates by track ID.
    Liked songs get priority (appear first).
    """
    seen_ids = set()
    unique = []
    for song in liked_songs + playlist_songs:
        if song['id'] not in seen_ids:
            seen_ids.add(song['id'])
            unique.append(song)
    logger.info(f"Deduplicated: {len(liked_songs)} liked + {len(playlist_songs)} playlist = {len(unique)} unique")
    return unique


# ---------------------------------------------------------------------------
# Summarization (for Prompt Builder mode)
# ---------------------------------------------------------------------------

def summarize_library(all_songs, playlists_meta, top_artists, top_tracks):
    """Compact text summary of the library for the prompt builder AI."""
    parts = []
    parts.append(f"=== LIBRARY ({len(all_songs)} unique songs total) ===")

    if all_songs:
        artist_songs = defaultdict(list)
        for s in all_songs:
            artist_songs[s['artist']].append(s['name'])
        sorted_artists = sorted(artist_songs.items(), key=lambda x: len(x[1]), reverse=True)
        parts.append("Songs by artist (most-saved first):")
        for artist, songs in sorted_artists[:40]:
            song_list = songs[:8]
            extra = f" (+{len(songs) - 8} more)" if len(songs) > 8 else ""
            parts.append(f"  - {artist} ({len(songs)} songs): {', '.join(song_list)}{extra}")

    if top_artists:
        parts.append(f"\n=== TOP ARTISTS ===")
        sorted_a = sorted(top_artists, key=lambda a: a['rank'])
        parts.append(f"Most listened: {', '.join(a['name'] for a in sorted_a[:25])}")
        all_genres = [g for a in top_artists for g in a['genres']]
        genre_counts = Counter(all_genres)
        total = sum(genre_counts.values())
        if total > 0:
            top_g = genre_counts.most_common(15)
            parts.append(f"Genre breakdown: {', '.join(f'{g} ({round(c/total*100)}%)' for g, c in top_g)}")

    if top_tracks:
        parts.append(f"\n=== MOST-PLAYED TRACKS ===")
        for t in top_tracks[:25]:
            parts.append(f"  - \"{t['name']}\" by {t['artist']}")

    if playlists_meta:
        parts.append(f"\n=== PLAYLISTS ({len(playlists_meta)} total) ===")
        for p in sorted(playlists_meta, key=lambda p: p['tracks'], reverse=True)[:20]:
            parts.append(f"  - \"{p['name']}\" — {p['tracks']} tracks")

    summary = "\n".join(parts)
    logger.info(f"Summary: {len(summary)} chars, ~{len(summary.split())} words")
    return summary


# ---------------------------------------------------------------------------
# Song list for AI (for Playlist Creator mode)
# ---------------------------------------------------------------------------

def build_song_list_for_ai(all_songs):
    """
    One song per line: "SPOTIFY_ID | Song Name — Artist Name"
    The AI references songs by ID when selecting tracks.
    """
    return "\n".join(f"{s['id']} | {s['name']} — {s['artist']}" for s in all_songs)


# ---------------------------------------------------------------------------
# LLM System Prompts
# ---------------------------------------------------------------------------

def build_system_prompt(library_summary):
    """System prompt for PROMPT BUILDER mode (existing feature)."""
    return f"""You are Playlist Buddy — a music-obsessed AI that helps people write incredible prompts for Spotify's AI playlist creation feature. You have deep knowledge of this user's actual Spotify library, and your job is to turn vague playlist ideas into detailed, specific prompts that produce amazing results.

=== THIS USER'S SPOTIFY LIBRARY ===
{library_summary}
=== END LIBRARY DATA ===

=== YOUR CONVERSATION APPROACH ===

STAGE 1 — OPENING (first message only):
- Brief warm greeting showing you know their taste
- Mention something specific from their library
- Ask ONE open question about what playlist they want, with 2-3 suggestions

STAGE 2 — DISCOVERY (2-3 messages):
Ask ONE focused question per message: vibe, mood, tempo, era, specific artists, etc.
Don't ask things you can already answer from their library.

STAGE 3 — DRAFT THE PROMPT:
Once you have enough info, create the prompt:
- Reference specific artists/songs/genres from their library
- 3-5 sentences, packed with detail
- Wrap in markers:
  [PLAYLIST_PROMPT]
  Your detailed prompt here...
  [/PLAYLIST_PROMPT]
- Ask: "Want me to tweak anything?"

STAGE 4 — REFINE: New complete prompt on changes, always in [PLAYLIST_PROMPT] tags.

RULES:
1. ALWAYS reference their actual library.
2. Keep messages SHORT (2-4 sentences, except the prompt).
3. ONE question per message.
4. Don't recite their library — use it naturally.
5. Be opinionated and creative.
6. "Start over" = reset cheerfully.
7. Prompt should work standalone."""


def build_creator_system_prompt(song_list_text, total_songs):
    """System prompt for PLAYLIST CREATOR mode (new feature)."""
    return f"""You are Playlist Buddy in Playlist Creator mode. You have the user's COMPLETE Spotify library ({total_songs} songs). Your job: help them build a real playlist by selecting songs from their collection.

=== THE USER'S COMPLETE LIBRARY ===
Format: SPOTIFY_ID | Song Name — Artist Name
{song_list_text}
=== END LIBRARY ===

=== HOW YOU WORK ===

STAGE 1 — UNDERSTAND:
User describes what they want (specific songs, mood, activity, genre, era).
Respond warmly. If clear enough, go straight to selection. Otherwise ask ONE question.

STAGE 2 — SELECT SONGS:
Search their ENTIRE library. Be thorough. Consider genre, mood, energy, artist similarity, hidden gems.

Output in this EXACT format:

[PLAYLIST_SELECTION]
playlist_name: Your Creative Playlist Name
songs:
SPOTIFY_ID_1
SPOTIFY_ID_2
SPOTIFY_ID_3
[/PLAYLIST_SELECTION]

After the block: brief summary (count, vibe, notable picks). Ask: "Want me to add or remove anything?"

STAGE 3 — REFINE:
On changes, output a NEW complete [PLAYLIST_SELECTION] block.

STAGE 4 — CREATE:
When happy, include final [PLAYLIST_SELECTION] and say "Hit Create Playlist whenever you're ready!"

RULES:
1. ONLY select songs from the library above. Never invent songs.
2. Use EXACT Spotify IDs from the library.
3. Typically 15-40 songs unless user specifies.
4. Consider playlist FLOW — order songs well.
5. Keep messages SHORT (2-4 sentences outside selection).
6. If a requested song isn't in their library, say so honestly.
7. Give creative, specific playlist names."""


# ---------------------------------------------------------------------------
# LLM Chat Function
# ---------------------------------------------------------------------------

def chat_with_llm(messages, system_prompt):
    """Send conversation to the LLM. Takes system prompt directly for mode flexibility."""
    import requests as http_requests

    if not LLM_API_KEY:
        return "I'm not connected to an AI service yet. Set the OPENAI_API_KEY environment variable."

    full_messages = [{"role": "system", "content": system_prompt}]
    full_messages.extend(messages)

    try:
        response = http_requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": full_messages,
                "max_tokens": 2000,     # Higher — song lists can be long
                "temperature": 0.7,
            },
            timeout=60,                  # Longer timeout for large libraries
        )
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']

    except http_requests.exceptions.Timeout:
        logger.error("LLM API timeout")
        return "Sorry, the AI took too long to respond. Please try again."
    except http_requests.exceptions.HTTPError as e:
        logger.error(f"LLM API HTTP error: {e}")
        return "Sorry, there was an error talking to the AI. Please try again."
    except Exception as e:
        logger.error(f"LLM API error: {e}")
        return "Sorry, something went wrong. Please try again."


# ---------------------------------------------------------------------------
# Helper: auto-rebuild cache if lost after redeploy
# ---------------------------------------------------------------------------

def _ensure_library_loaded(user_id):
    """Rebuild library cache if lost (e.g. after Render redeploy)."""
    if user_id in library_cache and user_id in full_library_cache:
        return True
    try:
        sp, sp_oauth, cache_handler = get_spotify()
        user = sp.current_user()
        user_id = user['id']

        liked = fetch_all_liked_songs(sp)
        pl_songs, pl_meta = fetch_all_playlist_songs(sp)
        top_a = fetch_top_artists(sp)
        top_t = fetch_top_tracks(sp)

        all_unique = deduplicate_songs(liked, pl_songs)
        summary = summarize_library(all_unique, pl_meta, top_a, top_t)

        library_cache[user_id] = summary
        full_library_cache[user_id] = all_unique
        session['spotify_user_id'] = user_id
        logger.info(f"Auto-rebuilt cache for {user_id}: {len(all_unique)} songs")
        return True
    except Exception as e:
        logger.error(f"Failed to rebuild library: {e}")
        return False


# ---------------------------------------------------------------------------
# Direct Spotify API calls (bypassing Spotipy for deprecated endpoints)
# ---------------------------------------------------------------------------

def create_playlist_direct(access_token, name, public=True, description=""):
    """
    Create a playlist using POST /me/playlists (the NEW endpoint).
    
    The old endpoint POST /users/{user_id}/playlists was REMOVED in
    Spotify's February 2026 API changes, which is why Spotipy's
    user_playlist_create() returns 403 Forbidden.
    """
    import requests as http_requests

    response = http_requests.post(
        "https://api.spotify.com/v1/me/playlists",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "name": name,
            "public": public,
            "description": description,
        },
    )
    response.raise_for_status()
    return response.json()


def add_items_to_playlist_direct(access_token, playlist_id, uris):
    """
    Add tracks using POST /playlists/{id}/items (the NEW endpoint).
    
    The old endpoint /playlists/{id}/tracks was RENAMED to /items
    in the February 2026 API changes. Spotipy's playlist_add_items()
    may still use the old /tracks path.
    """
    import requests as http_requests

    # Spotify allows max 100 URIs per request
    for i in range(0, len(uris), 100):
        batch = uris[i:i + 100]
        response = http_requests.post(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"uris": batch},
        )
        response.raise_for_status()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def home():
    if is_authenticated():
        return redirect(url_for('chat_page'))
    return render_template('landing.html')


@app.route('/login')
def login():
    sp, sp_oauth, cache_handler = get_spotify()
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)


@app.route('/callback')
def callback():
    sp, sp_oauth, cache_handler = get_spotify()
    code = request.args.get('code')
    if not code:
        return redirect(url_for('home'))
    sp_oauth.get_access_token(code)
    return redirect(url_for('chat_page'))


@app.route('/chat')
def chat_page():
    if not is_authenticated():
        return redirect(url_for('home'))
    sp, sp_oauth, cache_handler = get_spotify()
    user = sp.current_user()
    username = user.get('display_name') or user.get('id', 'Music Fan')
    avatar = None
    if user.get('images') and len(user['images']) > 0:
        avatar = user['images'][0].get('url')
    return render_template('chat.html', username=username, avatar=avatar)


@app.route('/api/load-library')
def load_library():
    """Fetch ALL library data for both modes. Called once on page load."""
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    sp, sp_oauth, cache_handler = get_spotify()
    try:
        user = sp.current_user()
        user_id = user['id']
    except Exception as e:
        logger.error(f"Could not get user ID: {e}")
        return jsonify({'error': 'Could not identify user'}), 500

    # Return cached if already loaded
    if user_id in library_cache and user_id in full_library_cache:
        return jsonify({
            'status': 'ready',
            'total_songs': len(full_library_cache[user_id]),
            'summary_preview': library_cache[user_id][:200] + '...'
        })

    # Fetch everything
    liked = fetch_all_liked_songs(sp)
    pl_songs, pl_meta = fetch_all_playlist_songs(sp)
    top_a = fetch_top_artists(sp)
    top_t = fetch_top_tracks(sp)

    all_unique = deduplicate_songs(liked, pl_songs)
    summary = summarize_library(all_unique, pl_meta, top_a, top_t)

    library_cache[user_id] = summary
    full_library_cache[user_id] = all_unique
    session['spotify_user_id'] = user_id

    return jsonify({
        'status': 'ready',
        'total_songs': len(all_unique),
        'summary_preview': summary[:200] + '...'
    })


@app.route('/api/chat', methods=['POST'])
def chat_api():
    """Chat endpoint for PROMPT BUILDER mode."""
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session.get('spotify_user_id', '')
    if not library_cache.get(user_id):
        if not _ensure_library_loaded(user_id):
            return jsonify({'error': 'Library not loaded. Please refresh.'}), 400

    data = request.get_json()
    if not data or 'messages' not in data:
        return jsonify({'error': 'No messages provided'}), 400

    messages = data['messages'][-20:]  # Keep last 20 for cost control
    system_prompt = build_system_prompt(library_cache[user_id])
    response_text = chat_with_llm(messages, system_prompt)
    return jsonify({'response': response_text})


@app.route('/api/creator-chat', methods=['POST'])
def creator_chat_api():
    """
    Chat endpoint for PLAYLIST CREATOR mode.
    Sends the FULL song list to the AI so it can select specific tracks.
    """
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session.get('spotify_user_id', '')
    if not full_library_cache.get(user_id):
        if not _ensure_library_loaded(user_id):
            return jsonify({'error': 'Library not loaded. Please refresh.'}), 400

    data = request.get_json()
    if not data or 'messages' not in data:
        return jsonify({'error': 'No messages provided'}), 400

    messages = data['messages'][-20:]
    all_songs = full_library_cache[user_id]
    song_list_text = build_song_list_for_ai(all_songs)
    system_prompt = build_creator_system_prompt(song_list_text, len(all_songs))
    response_text = chat_with_llm(messages, system_prompt)
    return jsonify({'response': response_text})


@app.route('/api/create-playlist', methods=['POST'])
def create_playlist_api():
    """
    Actually create a playlist on the user's Spotify account.
    
    Uses DIRECT API calls to Spotify instead of Spotipy, because:
    - Spotipy's user_playlist_create() hits POST /users/{id}/playlists
      which was REMOVED in Spotify's Feb 2026 API changes (returns 403)
    - The new endpoint is POST /me/playlists
    - Similarly, adding items uses /playlists/{id}/items (not /tracks)
    
    Expects: {"name": "Playlist Name", "song_ids": ["id1", "id2", ...]}
    Returns: {"success": true, "playlist_url": "https://open.spotify.com/..."}
    """
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    playlist_name = data.get('name', 'Playlist Buddy Mix')
    song_ids = data.get('song_ids', [])
    if not song_ids:
        return jsonify({'error': 'No songs provided'}), 400

    # Convert IDs to URIs (Spotify needs "spotify:track:ID" format)
    track_uris = [f"spotify:track:{tid}" for tid in song_ids]

    try:
        # Get a fresh access token for direct API calls
        access_token = get_access_token()

        # Create the playlist using the NEW endpoint (POST /me/playlists)
        playlist = create_playlist_direct(
            access_token=access_token,
            name=playlist_name,
            public=True,
            description="Created by Playlist Buddy",
        )
        playlist_id = playlist['id']
        playlist_url = playlist['external_urls']['spotify']

        # Add tracks using the NEW endpoint (/playlists/{id}/items)
        add_items_to_playlist_direct(access_token, playlist_id, track_uris)

        logger.info(f"Created '{playlist_name}' with {len(track_uris)} tracks")
        return jsonify({
            'success': True,
            'playlist_url': playlist_url,
            'playlist_name': playlist_name,
            'track_count': len(track_uris),
        })

    except Exception as e:
        logger.error(f"Error creating playlist: {e}")
        return jsonify({'error': f'Failed to create playlist: {str(e)}'}), 500


@app.route('/api/song-details', methods=['POST'])
def song_details_api():
    """
    Given song IDs, return names/artists from our cache.
    Used by the frontend to display the playlist preview.
    """
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session.get('spotify_user_id', '')
    all_songs = full_library_cache.get(user_id, [])
    if not all_songs:
        return jsonify({'error': 'Library not loaded'}), 400

    data = request.get_json()
    song_ids = data.get('song_ids', [])

    # Build lookup dict for fast access
    lookup = {s['id']: s for s in all_songs}
    details = []
    for sid in song_ids:
        song = lookup.get(sid)
        if song:
            details.append({
                'id': song['id'],
                'name': song['name'],
                'artist': song['artist'],
                'album': song['album'],
            })
        else:
            logger.warning(f"Song ID not found in cache: {sid}")
    
    logger.info(f"Song details: {len(details)} found out of {len(song_ids)} requested")
    return jsonify({'songs': details})


@app.route('/logout')
def logout():
    user_id = session.get('spotify_user_id', '')
    if user_id:
        library_cache.pop(user_id, None)
        full_library_cache.pop(user_id, None)
    session.clear()
    return redirect(url_for('home'))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
