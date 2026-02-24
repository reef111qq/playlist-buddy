"""
Spotify Playlist Prompt Builder
================================
A web app that connects to your Spotify library and uses an AI chatbot
to help you craft detailed prompts for Spotify's AI playlist feature.

Architecture:
- Flask handles web routes and session management
- Spotipy handles all Spotify API calls
- OpenAI (or compatible) API powers the chatbot
- Library data is fetched once, summarized, and fed to the chatbot as context
"""

import os
import json
import logging
from collections import Counter
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

# Server-side cache for library summaries (too large for session cookies)
# Key: Spotify user ID, Value: summary string
library_cache = {}

# Spotify credentials (set these as environment variables on Render)
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')

# We need these scopes:
#   user-library-read    → read liked/saved songs
#   playlist-read-private → read user's playlists
#   user-top-read        → read top artists/tracks (best signal for taste)
SCOPE = 'user-library-read playlist-read-private user-top-read'

# LLM settings
# Supports OpenAI-compatible APIs (OpenAI, OpenRouter, local, etc.)
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
    """
    Create a per-request Spotify client tied to the current user's session.
    
    Why per-request? Each user has their own OAuth token stored in their
    session cookie. We need to build a fresh client each time so we use
    the correct user's token.
    """
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
# Phase 1: Library Fetching
# ---------------------------------------------------------------------------

def fetch_liked_songs(sp, limit=500):
    """
    Fetch the user's liked/saved songs.
    
    We cap at 500 songs to keep things fast. For the chatbot, we don't need
    every song — we need enough to understand their taste patterns.
    
    Each API call fetches 50 songs, so 500 songs = 10 API calls.
    """
    songs = []
    try:
        results = sp.current_user_saved_tracks(limit=50)
        while results and len(songs) < limit:
            for item in results.get('items', []):
                track = item.get('track')
                if not track or not track.get('id'):
                    continue
                songs.append({
                    'name': track['name'],
                    'artist': track['artists'][0]['name'] if track.get('artists') else 'Unknown',
                    'album': track['album']['name'] if track.get('album') else 'Unknown',
                })
            if results.get('next') and len(songs) < limit:
                results = sp.next(results)
            else:
                break
    except Exception as e:
        logger.error(f"Error fetching liked songs: {e}")
    
    logger.info(f"Fetched {len(songs)} liked songs")
    return songs


def fetch_playlists(sp):
    """
    Fetch the user's playlists INCLUDING the songs inside them.
    
    For each playlist we grab:
    - The playlist name and track count
    - Up to 50 song names + artists from inside it
    
    This lets the chatbot reference specific songs the user has curated
    into playlists, not just the playlist names.
    """
    playlists = []
    try:
        results = sp.current_user_playlists(limit=50)
        while results:
            for item in results.get('items', []):
                if not item:
                    continue
                
                playlist_id = item.get('id')
                playlist_name = item.get('name', 'Untitled')
                track_count = item.get('tracks', {}).get('total', 0)
                
                # Fetch songs inside this playlist (up to 50 per playlist)
                songs = []
                if playlist_id and track_count > 0:
                    try:
                        tracks_result = sp.playlist_tracks(
                            playlist_id, 
                            limit=100,
                            fields='items(track(name,artists(name)))'
                        )
                        for t_item in tracks_result.get('items', []):
                            track = t_item.get('track')
                            if not track or not track.get('name'):
                                continue
                            artist = track['artists'][0]['name'] if track.get('artists') else 'Unknown'
                            songs.append({
                                'name': track['name'],
                                'artist': artist,
                            })
                    except Exception as e:
                        logger.error(f"Error fetching tracks for playlist '{playlist_name}': {e}")
                
                playlists.append({
                    'name': playlist_name,
                    'tracks': track_count,
                    'songs': songs,
                })

            if results.get('next'):
                results = sp.next(results)
            else:
                break
    except Exception as e:
        logger.error(f"Error fetching playlists: {e}")
    
    logger.info(f"Fetched {len(playlists)} playlists with songs")
    return playlists


def fetch_top_artists(sp):
    """
    Fetch the user's top artists across all time ranges.
    
    Spotify provides three time ranges:
      - short_term  → roughly last 4 weeks
      - medium_term → roughly last 6 months
      - long_term   → all time
    
    We merge all three to get a complete picture of their taste.
    Each call returns up to 50 artists. Genres come from the artist objects.
    """
    artists = {}  # keyed by artist ID to deduplicate
    
    for time_range in ['short_term', 'medium_term', 'long_term']:
        try:
            results = sp.current_user_top_artists(limit=50, time_range=time_range)
            for i, artist in enumerate(results.get('items', [])):
                aid = artist.get('id')
                if not aid:
                    continue
                # If we haven't seen this artist, add them.
                # If we have, keep the one with the better (lower) rank.
                if aid not in artists:
                    artists[aid] = {
                        'name': artist.get('name', 'Unknown'),
                        'genres': artist.get('genres', []),
                        'popularity': artist.get('popularity', 0),
                        'rank': i,  # position in the list (0 = most listened)
                        'time_range': time_range,
                    }
        except Exception as e:
            logger.error(f"Error fetching top artists ({time_range}): {e}")
    
    logger.info(f"Fetched {len(artists)} unique top artists")
    return list(artists.values())


def fetch_top_tracks(sp):
    """
    Fetch the user's top tracks (medium term).
    This tells us what specific songs they've been gravitating toward.
    """
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


# ---------------------------------------------------------------------------
# Phase 2: Library Summarization
# ---------------------------------------------------------------------------

def summarize_library(liked_songs, playlists, top_artists, top_tracks):
    """
    Crunch raw library data into a compact text summary for the chatbot.
    
    This is the most important function in the app. The summary needs to be:
    - Compact (under ~800 words — we're paying per token but need enough detail)
    - Signal-rich (genres, artists, actual song names, patterns)
    - Natural language (the LLM reads it as context, not structured data)
    
    The chatbot MUST know actual song and artist names from the user's library
    so it can reference them in prompts. Generic genre info isn't enough.
    """
    parts = []

    # --- Liked songs: group by artist so chatbot knows WHAT they listen to ---
    parts.append(f"=== LIKED SONGS ({len(liked_songs)} total) ===")

    if liked_songs:
        # Group songs by artist
        from collections import defaultdict
        artist_songs = defaultdict(list)
        for s in liked_songs:
            artist_songs[s['artist']].append(s['name'])

        # Sort artists by how many songs the user has saved (most → least)
        sorted_artists = sorted(artist_songs.items(), key=lambda x: len(x[1]), reverse=True)

        # Top 40 artists with their actual song names (up to 8 songs each)
        parts.append("Liked songs by artist (most-saved first):")
        for artist, songs in sorted_artists[:40]:
            song_list = songs[:8]
            extra = f" (+{len(songs) - 8} more)" if len(songs) > 8 else ""
            parts.append(f"  • {artist} ({len(songs)} songs): {', '.join(song_list)}{extra}")

    # --- Top artists + genre analysis ---
    if top_artists:
        parts.append(f"\n=== TOP ARTISTS (from Spotify listening history) ===")
        sorted_artists = sorted(top_artists, key=lambda a: a['rank'])
        top_names = [a['name'] for a in sorted_artists[:25]]
        parts.append(f"Most listened: {', '.join(top_names)}")

        # Genre breakdown
        all_genres = []
        for a in top_artists:
            all_genres.extend(a['genres'])
        genre_counts = Counter(all_genres)
        total_genre_tags = sum(genre_counts.values())
        
        if total_genre_tags > 0:
            top_genres = genre_counts.most_common(15)
            genre_parts = []
            for genre, count in top_genres:
                pct = round(count / total_genre_tags * 100)
                genre_parts.append(f"{genre} ({pct}%)")
            parts.append(f"Genre breakdown: {', '.join(genre_parts)}")

    # --- Top tracks (what they're playing RIGHT NOW) ---
    if top_tracks:
        parts.append(f"\n=== CURRENTLY MOST-PLAYED TRACKS ===")
        for t in top_tracks[:25]:
            parts.append(f"  • \"{t['name']}\" by {t['artist']}")

    # --- Playlists with their actual songs ---
    if playlists:
        parts.append(f"\n=== PLAYLISTS ({len(playlists)} total) ===")
        # Sort by track count to show the playlists they've invested most in
        sorted_playlists = sorted(playlists, key=lambda p: p['tracks'], reverse=True)
        for p in sorted_playlists[:20]:
            songs = p.get('songs', [])
            if songs:
                # Show up to 15 songs per playlist
                song_strs = [f"{s['name']} – {s['artist']}" for s in songs[:15]]
                extra = f" (+{len(songs) - 15} more)" if len(songs) > 15 else ""
                parts.append(f"  • \"{p['name']}\" ({p['tracks']} tracks): {', '.join(song_strs)}{extra}")
            else:
                parts.append(f"  • \"{p['name']}\" — {p['tracks']} tracks")

    summary = "\n".join(parts)
    logger.info(f"Library summary: {len(summary)} chars, ~{len(summary.split())} words")
    return summary


# ---------------------------------------------------------------------------
# Phase 3: Chatbot (LLM Integration)
# ---------------------------------------------------------------------------

def build_system_prompt(library_summary):
    """
    Build the system prompt that tells the LLM who it is and what it knows.
    
    The system prompt has two parts:
    1. Instructions — how the chatbot should behave
    2. User's library summary — injected as context so the bot "knows" their taste
    """
    return f"""You are Playlist Buddy — a music-obsessed AI that helps people write incredible prompts for Spotify's AI playlist creation feature. You have deep knowledge of this user's actual Spotify library, and your job is to turn vague playlist ideas into detailed, specific prompts that produce amazing results.

=== THIS USER'S SPOTIFY LIBRARY ===
{library_summary}
=== END LIBRARY DATA ===

=== YOUR CONVERSATION APPROACH ===

You guide the user through a focused, friendly conversation to build the perfect playlist prompt. Follow these stages:

STAGE 1 — OPENING (first message only):
- Give a brief, warm greeting that shows you already know their taste
- Pick out something specific from their library to mention ("I can see you've got great taste — lots of [artist] and [artist]...")
- Ask ONE open question: what kind of playlist are they thinking about? Give 2-3 quick suggestions based on what you see in their library to spark ideas (e.g., "Maybe a late-night R&B mix around your Frank Ocean tracks? Or something upbeat pulling from your indie collection?")

STAGE 2 — DISCOVERY (2-3 messages):
Ask ONE focused question per message. Pick from these based on what you still need to know:
- "What's the vibe or mood? (e.g., melancholic, hype, dreamy, aggressive, cozy)"
- "Is this for a specific moment? (driving, working out, cooking, winding down, a party)"
- "Do you want mostly songs you already know, or a mix of familiar + discovery?"
- "Any tempo preference? (slow and chill, mid-tempo groove, high energy)"
- "Any specific era? (90s throwbacks, 2010s hits, brand new releases, mix of everything)"
- "Any artists you definitely want included or specifically excluded?"
- "Should it stay in one lane or blend genres?"
Don't ask questions you can already answer from their library. If they say "chill" and you can see they have a playlist called "Late Night Drives" full of R&B, connect those dots yourself.

STAGE 3 — DRAFT THE PROMPT:
Once you have enough info (usually after 2-3 questions), create the prompt. Rules:
- The prompt MUST reference specific artists, songs, or genres from their actual library when relevant
- The prompt should be 3-5 sentences, packed with detail: genres, subgenres, moods, tempos, energy arc, eras, reference artists, and thematic elements
- Write it as a natural paragraph the user can paste directly into Spotify's AI feature
- Wrap it in markers so the app can style it with a copy button:
  [PLAYLIST_PROMPT]
  Your detailed prompt here...
  [/PLAYLIST_PROMPT]
- After the prompt, ask a short follow-up: "Want me to tweak anything? I can make it more upbeat, add some discovery, shift the era, etc."

STAGE 4 — REFINE (if needed):
- When they ask for changes, produce a NEW complete prompt (don't describe the changes, just show the updated version)
- Always wrap refined prompts in [PLAYLIST_PROMPT] tags too

=== CRITICAL RULES ===

1. ALWAYS PULL FROM THEIR LIBRARY. This is the entire point of the app. Reference their actual artists, songs, playlists, and genres — not generic suggestions. If they want a chill playlist and they have The Weeknd, Daniel Caesar, and SZA in their library, mention those artists by name in the prompt.

2. Keep messages SHORT. 2-4 sentences max per response (except the prompt itself). Don't write essays. Be punchy and conversational.

3. ONE question per message during discovery. Never dump 5 questions at once — it kills the conversational feel.

4. Don't recite their library back to them. Use it naturally: "Since you're into [artist], we could lean into that sound..." — not "I can see you have 47 songs by [artist] in your library."

5. Be opinionated and creative. Suggest angles they haven't thought of. "You've got a lot of [genre] but I noticed some [unexpected genre] in there too — want to blend those?"

6. If the user says "start over" or wants a new playlist, reset cheerfully and go back to Stage 1.

7. The generated prompt should work standalone — someone reading just the prompt (without our conversation) should understand exactly what playlist is being requested."""


def chat_with_llm(messages, library_summary):
    """
    Send a conversation to the LLM and get a response.
    
    We use the 'requests' library directly instead of the OpenAI SDK
    to keep dependencies minimal and support any OpenAI-compatible API.
    
    Args:
        messages: list of {"role": "user"|"assistant", "content": "..."} dicts
        library_summary: the compact library summary string
    
    Returns:
        The assistant's response text, or an error message.
    """
    import requests as http_requests  # renamed to avoid clash with flask.request

    if not LLM_API_KEY:
        return "I'm not connected to an AI service yet. The app owner needs to set the OPENAI_API_KEY environment variable."

    system_prompt = build_system_prompt(library_summary)

    # Build the full message list: system prompt + conversation history
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
                "max_tokens": 800,       # Enough for detailed prompt + conversation
                "temperature": 0.8,      # Slightly creative but still focused
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']

    except http_requests.exceptions.Timeout:
        logger.error("LLM API timeout")
        return "Sorry, the AI took too long to respond. Please try again."
    except http_requests.exceptions.HTTPError as e:
        logger.error(f"LLM API HTTP error: {e} — {e.response.text if e.response else 'no body'}")
        return "Sorry, there was an error talking to the AI. Please try again."
    except Exception as e:
        logger.error(f"LLM API error: {e}")
        return "Sorry, something went wrong. Please try again."


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def home():
    """Landing page — if authenticated, go to chat. Otherwise show login."""
    if is_authenticated():
        return redirect(url_for('chat_page'))
    return render_template('landing.html')


@app.route('/login')
def login():
    """Redirect to Spotify's authorization page."""
    sp, sp_oauth, cache_handler = get_spotify()
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)


@app.route('/callback')
def callback():
    """
    Spotify redirects here after the user approves access.
    We exchange the authorization code for an access token.
    """
    sp, sp_oauth, cache_handler = get_spotify()
    code = request.args.get('code')
    if not code:
        return redirect(url_for('home'))
    sp_oauth.get_access_token(code)
    return redirect(url_for('chat_page'))


@app.route('/chat')
def chat_page():
    """The main app page — chat interface."""
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
    """
    Fetches ALL library data and builds the summary.
    Called once when the chat page loads.
    
    Returns the summary text so the frontend knows it's ready.
    """
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    sp, sp_oauth, cache_handler = get_spotify()

    # Get user ID for cache key
    try:
        user = sp.current_user()
        user_id = user['id']
    except Exception as e:
        logger.error(f"Could not get user ID: {e}")
        return jsonify({'error': 'Could not identify user'}), 500

    # Return cached summary if we already built it
    if user_id in library_cache:
        return jsonify({'status': 'ready', 'summary_preview': library_cache[user_id][:200] + '...'})

    # Fetch all data (Phase 1)
    liked_songs = fetch_liked_songs(sp, limit=3000)
    playlists = fetch_playlists(sp)
    top_artists = fetch_top_artists(sp)
    top_tracks = fetch_top_tracks(sp)

    # Summarize (Phase 2)
    summary = summarize_library(liked_songs, playlists, top_artists, top_tracks)

    # Cache server-side (not in session — too large for cookies)
    library_cache[user_id] = summary
    session['spotify_user_id'] = user_id

    return jsonify({'status': 'ready', 'summary_preview': summary[:200] + '...'})


@app.route('/api/chat', methods=['POST'])
def chat_api():
    """
    Chat endpoint — receives user message, returns AI response.
    
    Expects JSON: {"messages": [{"role": "user", "content": "..."}, ...]}
    The frontend sends the FULL conversation history each time.
    
    Returns JSON: {"response": "assistant's message"}
    """
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session.get('spotify_user_id', '')
    library_summary = library_cache.get(user_id, '')
    
    # If cache was lost (e.g. after redeploy), rebuild it automatically
    if not library_summary:
        try:
            sp, sp_oauth, cache_handler = get_spotify()
            user = sp.current_user()
            user_id = user['id']
            
            liked_songs = fetch_liked_songs(sp, limit=3000)
            playlists = fetch_playlists(sp)
            top_artists = fetch_top_artists(sp)
            top_tracks = fetch_top_tracks(sp)
            
            library_summary = summarize_library(liked_songs, playlists, top_artists, top_tracks)
            library_cache[user_id] = library_summary
            session['spotify_user_id'] = user_id
            logger.info(f"Auto-rebuilt library cache for {user_id}")
        except Exception as e:
            logger.error(f"Failed to auto-rebuild library: {e}")
            return jsonify({'error': 'Library not loaded yet. Please refresh the page.'}), 400

    data = request.get_json()
    if not data or 'messages' not in data:
        return jsonify({'error': 'No messages provided'}), 400

    messages = data['messages']

    # Safety: limit conversation length to control costs
    # Keep only last 20 messages (10 exchanges)
    if len(messages) > 20:
        messages = messages[-20:]

    response_text = chat_with_llm(messages, library_summary)

    return jsonify({'response': response_text})


@app.route('/logout')
def logout():
    user_id = session.get('spotify_user_id', '')
    if user_id and user_id in library_cache:
        del library_cache[user_id]
    session.clear()
    return redirect(url_for('home'))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
