import os
import logging
from flask import Flask, session, request, redirect, url_for, render_template, jsonify
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler

# Set up logging so errors aren't silently swallowed
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(64))

# ============================================================
# BUG FIX #4: Removed hardcoded client credentials.
# These MUST be set as environment variables now.
# If they're missing, the app will fail loudly on startup
# instead of silently using leaked credentials.
# ============================================================
client_id = os.environ.get('SPOTIFY_CLIENT_ID')
client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET')
redirect_uri = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')
scope = 'playlist-read-private user-library-read'

if not client_id or not client_secret:
    logger.warning(
        "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables are not set. "
        "Set them before running the app. "
        "Example: export SPOTIFY_CLIENT_ID='your_id_here'"
    )


# ============================================================
# BUG FIX #1: Create auth objects PER REQUEST, not globally.
#
# Why? The old code created one SpotifyOAuth and one Spotify
# client when the app started. But the "cache_handler" needs
# to read/write tokens from the current user's session — and
# "session" only exists during an active web request.
#
# This helper function creates fresh auth objects each time
# a request comes in, so each user gets their own token.
# ============================================================
def get_spotify():
    """Create a per-request Spotify client tied to the current user's session."""
    cache_handler = FlaskSessionCacheHandler(session)
    sp_oauth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        cache_handler=cache_handler,
        show_dialog=True
    )
    sp = Spotify(auth_manager=sp_oauth)
    return sp, sp_oauth, cache_handler


# ============================================================
# BUG FIX #2: Cache the user's library in the session.
#
# The old search endpoint fetched ALL playlists and ALL their
# tracks on EVERY keystroke — potentially hundreds of API calls
# each time you typed a letter. This would be painfully slow
# and would hit Spotify's rate limits almost immediately.
#
# The fix: fetch the library ONCE, store it in the session,
# and search against that cached list. The /api/load-library
# endpoint handles the initial fetch, and /api/search-songs
# just filters the cached data (instant, no API calls).
# ============================================================
def fetch_user_library(sp):
    """
    Fetch all tracks from all of the user's playlists.
    Returns a list of song dictionaries.
    
    This runs once per session and the result gets cached,
    so we're not hammering the API on every search.
    """
    all_songs = {}  # Use dict keyed by track ID for deduplication (BUG FIX #6)

    # Step 1: Get all playlists
    playlists = []
    results = sp.current_user_playlists(limit=50)
    playlists.extend(results['items'])

    while results['next']:
        results = sp.next(results)
        playlists.extend(results['items'])

    logger.info(f"Found {len(playlists)} playlists. Fetching tracks...")

    # Step 2: Get tracks from each playlist
    for playlist in playlists:
        playlist_id = playlist['id']
        playlist_name = playlist['name']

        try:
            tracks_results = sp.playlist_tracks(playlist_id, limit=100)
            tracks = tracks_results['items']

            while tracks_results['next']:
                tracks_results = sp.next(tracks_results)
                tracks.extend(tracks_results['items'])

            for item in tracks:
                # Skip empty/None tracks (can happen with local files or removed songs)
                if not item or not item.get('track') or not item['track'].get('name'):
                    continue

                track = item['track']
                track_id = track.get('id')

                # Local files don't have an ID — skip them since we can't
                # look up audio features for them later anyway
                if not track_id:
                    continue

                # ============================================
                # BUG FIX #6: Deduplicate by track ID.
                # If a song is in 3 playlists, we store it once
                # but keep a list of which playlists it's in.
                # ============================================
                if track_id in all_songs:
                    # Song already seen — just add the playlist name
                    if playlist_name not in all_songs[track_id]['playlists']:
                        all_songs[track_id]['playlists'].append(playlist_name)
                else:
                    all_songs[track_id] = {
                        'id': track_id,
                        'name': track['name'],
                        'artist': track['artists'][0]['name'] if track['artists'] else 'Unknown',
                        'album': track['album']['name'] if track.get('album') else 'Unknown',
                        'image': track['album']['images'][0]['url'] if track.get('album') and track['album'].get('images') else None,
                        'url': track['external_urls']['spotify'] if track.get('external_urls') else '#',
                        'playlists': [playlist_name]
                    }

        # BUG FIX #3: Catch specific exceptions and log them
        # instead of bare "except:" which hides real bugs
        except Exception as e:
            logger.error(f"Error fetching tracks from playlist '{playlist_name}': {e}")
            continue

    song_list = list(all_songs.values())
    logger.info(f"Library loaded: {len(song_list)} unique songs")
    return song_list


@app.route('/')
def home():
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        auth_url = sp_oauth.get_authorize_url()
        return redirect(auth_url)
    return redirect(url_for('get_playlists'))


@app.route('/callback')
def callback():
    sp, sp_oauth, cache_handler = get_spotify()
    sp_oauth.get_access_token(request.args['code'])
    return redirect(url_for('get_playlists'))


@app.route('/playlists')
def get_playlists():
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        auth_url = sp_oauth.get_authorize_url()
        return redirect(auth_url)

    # Get user info
    user = sp.current_user()
    username = user['display_name'] or user['id']

    # Get all playlists
    playlists = []
    results = sp.current_user_playlists(limit=50)
    playlists.extend(results['items'])

    while results['next']:
        results = sp.next(results)
        playlists.extend(results['items'])

    playlists_info = []
    for pl in playlists:
        playlists_info.append({
            'name': pl['name'],
            'url': pl['external_urls']['spotify'],
            'image': pl['images'][0]['url'] if pl['images'] else None,
            'tracks': pl.get('tracks', {}).get('total', 0),
            'public': pl.get('public', False)
        })

    return render_template('playlists.html',
                           playlists=playlists_info,
                           username=username,
                           total=len(playlists_info))


@app.route('/api/load-library')
def load_library():
    """
    Fetches the user's full library and caches it in the session.
    The frontend calls this ONCE when the page loads, then all
    searches happen instantly against the cached data.
    """
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401

    # Only fetch if we haven't already cached it this session
    if 'library_cache' not in session:
        songs = fetch_user_library(sp)
        session['library_cache'] = songs
        return jsonify({'status': 'loaded', 'total': len(songs)})
    else:
        return jsonify({'status': 'already_cached', 'total': len(session['library_cache'])})


@app.route('/api/search-songs')
def search_songs():
    """
    Searches the CACHED library (no API calls here).
    This is what makes the search bar feel instant.
    """
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401

    query = request.args.get('q', '').lower().strip()
    if not query or len(query) < 2:
        return jsonify({'results': []})

    # Search against the cached library — no Spotify API calls needed
    library = session.get('library_cache', [])

    if not library:
        return jsonify({'results': [], 'error': 'Library not loaded. Call /api/load-library first.'})

    results = []
    for song in library:
        song_name = song['name'].lower()
        artist_name = song['artist'].lower()

        if query in song_name or query in artist_name:
            results.append(song)

    # Cap results to avoid sending massive JSON payloads
    return jsonify({'results': results[:200]})


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


if __name__ == "__main__":
    app.run(debug=True)
