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
    Fetch the user's playlists (name + track count only).
    Playlist names tell the chatbot a lot about the user's listening contexts
    (e.g., "Workout", "Rainy Day", "Road Trip").
    """
    playlists = []
    try:
        results = sp.current_user_playlists(limit=50)
        while results:
            for item in results.get('items', []):
                if not item:
                    continue
                playlists.append({
                    'name': item.get('name', 'Untitled'),
                    'tracks': item.get('tracks', {}).get('total', 0),
                })
            if results.get('next'):
                results = sp.next(results)
            else:
                break
    except Exception as e:
        logger.error(f"Error fetching playlists: {e}")
    
    logger.info(f"Fetched {len(playlists)} playlists")
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
    - Compact (under 500 words — we're paying per token)
    - Signal-rich (genres, artists, patterns, not raw data)
    - Natural language (the LLM reads it as context, not structured data)
    """
    parts = []

    # --- Liked songs stats ---
    parts.append(f"The user has {len(liked_songs)} liked/saved songs in their library.")

    # Count artists across liked songs
    if liked_songs:
        artist_counts = Counter(s['artist'] for s in liked_songs)
        top_liked_artists = artist_counts.most_common(15)
        artists_str = ", ".join(f"{name} ({count})" for name, count in top_liked_artists)
        parts.append(f"Most-saved artists in liked songs: {artists_str}.")

    # --- Top artists + genre analysis ---
    if top_artists:
        # Top artist names
        sorted_artists = sorted(top_artists, key=lambda a: a['rank'])
        top_names = [a['name'] for a in sorted_artists[:20]]
        parts.append(f"Top listened artists (Spotify algorithm): {', '.join(top_names)}.")

        # Genre breakdown
        all_genres = []
        for a in top_artists:
            all_genres.extend(a['genres'])
        genre_counts = Counter(all_genres)
        total_genre_tags = sum(genre_counts.values())
        
        if total_genre_tags > 0:
            # Show top genres with rough percentages
            top_genres = genre_counts.most_common(12)
            genre_parts = []
            for genre, count in top_genres:
                pct = round(count / total_genre_tags * 100)
                genre_parts.append(f"{genre} ({pct}%)")
            parts.append(f"Genre breakdown from top artists: {', '.join(genre_parts)}.")

    # --- Top tracks ---
    if top_tracks:
        track_strs = [f'"{t["name"]}" by {t["artist"]}' for t in top_tracks[:10]]
        parts.append(f"Currently most-played tracks: {', '.join(track_strs)}.")

    # --- Playlists ---
    if playlists:
        parts.append(f"The user has {len(playlists)} playlists.")
        # Playlist names reveal listening contexts
        playlist_strs = [f'"{p["name"]}" ({p["tracks"]} tracks)' for p in playlists[:15]]
        parts.append(f"Notable playlists: {', '.join(playlist_strs)}.")

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
    return f"""You are a Spotify playlist prompt crafting assistant. Your job is to help users write detailed, specific prompts they can paste into Spotify's AI playlist creation feature to get amazing results.

You have access to this user's Spotify listening data:

---
{library_summary}
---

HOW TO BEHAVE:
- Be warm, enthusiastic, and music-savvy. You love helping people discover the perfect playlist.
- Start by asking what kind of playlist they want to create. Ask about the vibe, occasion, or mood.
- Ask 2-3 focused clarifying questions to dial in specifics (tempo, era, energy level, discovery vs. familiar, any particular artists to include/exclude).
- Don't ask all questions at once — keep it conversational, 1-2 questions per message.
- Reference their actual taste when relevant ("Since you listen to a lot of Radiohead, we could lean into that art-rock sound...").
- When you have enough info, produce a FINAL PROMPT — a detailed, polished paragraph the user can copy-paste into Spotify's playlist AI.

FINAL PROMPT FORMAT:
- When you're ready to deliver the prompt, wrap it in a special marker so the app can style it:
  [PLAYLIST_PROMPT]
  Your detailed prompt text here...
  [/PLAYLIST_PROMPT]
- The prompt should be 2-4 sentences, rich with specifics: genres, moods, tempos, eras, reference artists, energy arc, and any thematic elements.
- After showing the prompt, ask if they want to tweak anything.

IMPORTANT RULES:
- Never include raw data dumps. You're a creative collaborator, not a data report.
- Keep messages concise — 2-4 sentences per response (except the final prompt).
- If the user asks to start over, cheerfully reset and ask what playlist they want to make.
- You can suggest creative angles they haven't thought of based on their listening patterns."""


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
                "max_tokens": 600,       # Enough for a detailed prompt response
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

    # Return cached summary if we already built it
    cached_summary = session.get('library_summary')
    if cached_summary:
        return jsonify({'status': 'ready', 'summary_preview': cached_summary[:200] + '...'})

    sp, sp_oauth, cache_handler = get_spotify()

    # Fetch all data (Phase 1)
    liked_songs = fetch_liked_songs(sp, limit=500)
    playlists = fetch_playlists(sp)
    top_artists = fetch_top_artists(sp)
    top_tracks = fetch_top_tracks(sp)

    # Summarize (Phase 2)
    summary = summarize_library(liked_songs, playlists, top_artists, top_tracks)

    # Cache in session
    session['library_summary'] = summary

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

    library_summary = session.get('library_summary', '')
    if not library_summary:
        return jsonify({'error': 'Library not loaded yet. Please wait a moment and try again.'}), 400

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
    session.clear()
    return redirect(url_for('home'))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
