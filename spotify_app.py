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

# Credentials from environment variables (set in Render dashboard)
client_id = os.environ.get('SPOTIFY_CLIENT_ID')
client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET')
redirect_uri = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')
scope = 'playlist-read-private user-library-read'

if not client_id or not client_secret:
    logger.warning(
        "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables are not set. "
        "Set them before running the app."
    )


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
# CHANGED APPROACH: Fetch "Liked Songs" instead of all playlists.
#
# Why? The old approach looped through every playlist and fetched
# every track from each one. If you have 50 playlists, that's
# 50+ API calls just for the playlist contents — way too slow
# and likely to timeout on Render's free tier.
#
# "Liked Songs" (saved tracks) is a single paginated endpoint
# that returns 50 songs per call. 1000 liked songs = 20 calls.
# Much faster, much less likely to timeout.
#
# We also fetch playlist tracks, but we do both and combine them.
# ============================================================
def fetch_user_library(sp):
    """
    Fetch all of the user's saved/liked tracks AND playlist tracks.
    Returns a list of song dictionaries.
    """
    all_songs = {}  # Keyed by track ID for deduplication

    # ---- PART 1: Liked/Saved Songs (fast — single paginated endpoint) ----
    logger.info("Fetching liked/saved songs...")
    try:
        results = sp.current_user_saved_tracks(limit=50)
        while True:
            for item in results['items']:
                if not item or not item.get('track'):
                    continue
                track = item['track']
                track_id = track.get('id')
                if not track_id or not track.get('name'):
                    continue

                all_songs[track_id] = {
                    'id': track_id,
                    'name': track['name'],
                    'artist': track['artists'][0]['name'] if track['artists'] else 'Unknown',
                    'album': track['album']['name'] if track.get('album') else 'Unknown',
                    'image': track['album']['images'][0]['url'] if track.get('album') and track['album'].get('images') else None,
                    'url': track['external_urls']['spotify'] if track.get('external_urls') else '#',
                    'playlists': ['Liked Songs']
                }

            # Move to next page, or stop if there are no more
            if results['next']:
                results = sp.next(results)
            else:
                break

        logger.info(f"Fetched {len(all_songs)} liked songs")
    except Exception as e:
        logger.error(f"Error fetching liked songs: {e}")

    # ---- PART 2: Playlist tracks (adds songs not in Liked Songs) ----
    logger.info("Fetching playlist tracks...")
    try:
        playlists = []
        pl_results = sp.current_user_playlists(limit=50)
        playlists.extend(pl_results['items'])

        while pl_results['next']:
            pl_results = sp.next(pl_results)
            playlists.extend(pl_results['items'])

        logger.info(f"Found {len(playlists)} playlists")

        for playlist in playlists:
            playlist_id = playlist['id']
            playlist_name = playlist['name']

            try:
                tracks_results = sp.playlist_tracks(playlist_id, limit=100)
                while True:
                    for item in tracks_results['items']:
                        if not item or not item.get('track'):
                            continue
                        track = item['track']
                        track_id = track.get('id')
                        if not track_id or not track.get('name'):
                            continue

                        if track_id in all_songs:
                            # Already have this song — just add the playlist name
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

                    if tracks_results['next']:
                        tracks_results = sp.next(tracks_results)
                    else:
                        break

            except Exception as e:
                logger.error(f"Error fetching tracks from playlist '{playlist_name}': {e}")
                continue

    except Exception as e:
        logger.error(f"Error fetching playlists: {e}")

    song_list = list(all_songs.values())
    logger.info(f"Library loaded: {len(song_list)} unique songs total")
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

    user = sp.current_user()
    username = user['display_name'] or user['id']

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
    The frontend calls this ONCE when the page loads.
    """
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401

    # If we already have a NON-EMPTY cache, return it
    cached = session.get('library_cache')
    if cached and len(cached) > 0:
        return jsonify({'status': 'already_cached', 'total': len(cached)})

    # Otherwise fetch fresh (this also re-fetches if last attempt got 0 songs)
    songs = fetch_user_library(sp)

    # Only cache if we actually got songs — don't save an empty list
    if songs:
        session['library_cache'] = songs

    return jsonify({'status': 'loaded', 'total': len(songs)})


@app.route('/api/search-songs')
def search_songs():
    """
    Searches the CACHED library (no API calls here — instant).
    """
    sp, sp_oauth, cache_handler = get_spotify()
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401

    query = request.args.get('q', '').lower().strip()
    if not query or len(query) < 2:
        return jsonify({'results': []})

    library = session.get('library_cache', [])

    if not library:
        return jsonify({'results': [], 'error': 'Library not loaded. Call /api/load-library first.'})

    results = []
    for song in library:
        song_name = song['name'].lower()
        artist_name = song['artist'].lower()

        if query in song_name or query in artist_name:
            results.append(song)

    return jsonify({'results': results[:200]})


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


if __name__ == "__main__":
    app.run(debug=True)
