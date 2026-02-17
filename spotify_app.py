import os
import logging
from flask import Flask, session, request, redirect, url_for, render_template, jsonify
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(64))

client_id = os.environ.get('SPOTIFY_CLIENT_ID')
client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET')
redirect_uri = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')
scope = 'playlist-read-private user-library-read'

if not client_id or not client_secret:
    logger.warning("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET not set.")


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


def fetch_liked_songs(sp):
    """
    Fetch ONLY the user's Liked/Saved songs.
    
    This is fast because it's a single paginated endpoint:
    - 50 songs per API call
    - 1000 liked songs = 20 API calls
    - 2000 liked songs = 40 API calls
    
    Much faster than crawling every playlist.
    """
    all_songs = []

    logger.info("Fetching liked/saved songs...")
    try:
        results = sp.current_user_saved_tracks(limit=50)
        page = 1
        while True:
            for item in results['items']:
                if not item or not item.get('track'):
                    continue
                track = item['track']
                track_id = track.get('id')
                if not track_id or not track.get('name'):
                    continue

                all_songs.append({
                    'id': track_id,
                    'name': track['name'],
                    'artist': track['artists'][0]['name'] if track['artists'] else 'Unknown',
                    'album': track['album']['name'] if track.get('album') else 'Unknown',
                    'image': track['album']['images'][0]['url'] if track.get('album') and track['album'].get('images') else None,
                    'url': track['external_urls']['spotify'] if track.get('external_urls') else '#',
                })

            logger.info(f"  Page {page}: {len(all_songs)} songs so far...")
            page += 1

            if results['next']:
                results = sp.next(results)
            else:
                break

    except Exception as e:
        logger.error(f"Error fetching liked songs: {e}")

    logger.info(f"Done! Loaded {len(all_songs)} liked songs.")
    return all_songs


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

    user = sp.current_user()
    username = user['display_name'] or user['id']

    return render_template('playlists.html', username=username)


@app.route('/api/load-library')
def load_library():
    """
    Fetches the user's liked songs and caches them in the session.
    Called ONCE when the page loads.
    """
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401

    # Return existing cache if we have songs
    cached = session.get('library_cache')
    if cached and len(cached) > 0:
        return jsonify({'status': 'already_cached', 'total': len(cached)})

    # Fetch fresh
    songs = fetch_liked_songs(sp)

    # Only cache if we got songs
    if songs:
        session['library_cache'] = songs

    return jsonify({'status': 'loaded', 'total': len(songs)})


@app.route('/api/search-songs')
def search_songs():
    """Searches the cached library — no API calls, instant."""
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401

    query = request.args.get('q', '').lower().strip()
    if not query or len(query) < 2:
        return jsonify({'results': []})

    library = session.get('library_cache', [])
    if not library:
        return jsonify({'results': [], 'error': 'Library not loaded yet.'})

    results = []
    for song in library:
        if query in song['name'].lower() or query in song['artist'].lower():
            results.append(song)

    return jsonify({'results': results[:200]})


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


if __name__ == "__main__":
    app.run(debug=True)
