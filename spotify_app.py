"""
Playlist Buddy — "Beef Up" Your Playlists
==========================================
IMPORTANT: Library loading runs in a background thread to avoid
gunicorn worker timeouts. Frontend polls /api/load-library for status.

Genre fetching now happens during background library load, not on-demand.
This avoids timeout issues and rate limits during playlist analysis.
"""

import os, json, re, logging, threading, time
from collections import Counter, defaultdict
from flask import Flask, session, request, redirect, url_for, render_template, jsonify
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler
import requests as http_requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(64))

# Server-side caches keyed by Spotify user ID
library_cache = {}
full_library_cache = {}
playlists_cache = {}
artist_genre_index_cache = {}

# Track background loading state: user_id -> {'status': 'loading'|'ready'|'error', 'message': '...'}
loading_state = {}

CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')
SCOPE = 'user-library-read playlist-read-private user-top-read playlist-modify-public playlist-modify-private'
LLM_API_KEY = os.environ.get('OPENAI_API_KEY', '')
LLM_MODEL = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
API_BASE = "https://api.spotify.com/v1"


# ─── Auth ───

def get_spotify():
    ch = FlaskSessionCacheHandler(session)
    oauth = SpotifyOAuth(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                         redirect_uri=REDIRECT_URI, scope=SCOPE,
                         cache_handler=ch, show_dialog=True)
    return Spotify(auth_manager=oauth), oauth, ch

def get_access_token():
    ch = FlaskSessionCacheHandler(session)
    oauth = SpotifyOAuth(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                         redirect_uri=REDIRECT_URI, scope=SCOPE, cache_handler=ch)
    ti = ch.get_cached_token()
    if not ti: return None
    if oauth.is_token_expired(ti):
        ti = oauth.refresh_access_token(ti['refresh_token'])
        ch.save_token_to_cache(ti)
    return ti['access_token']

def is_authenticated():
    ch = FlaskSessionCacheHandler(session)
    oauth = SpotifyOAuth(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                         redirect_uri=REDIRECT_URI, scope=SCOPE, cache_handler=ch)
    return oauth.validate_token(ch.get_cached_token()) is not None


# ─── Direct API helpers ───

def make_auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def api_get(token, path, params=None):
    try:
        r = http_requests.get(f"{API_BASE}{path}", headers=make_auth_headers(token), params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API GET {path}: {e}")
        return None

def api_get_url(token, url):
    try:
        r = http_requests.get(url, headers=make_auth_headers(token), timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API GET url: {e}")
        return None

def api_post(token, path, data=None):
    r = http_requests.post(f"{API_BASE}{path}", headers=make_auth_headers(token), json=data, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


# ─── Library Fetching ───

def fetch_all_liked_songs_direct(token):
    songs = []
    data = api_get(token, "/me/tracks", params={"limit": 50})
    while data:
        for item in data.get('items', []):
            t = item.get('track')
            if not t or not t.get('id'): continue
            songs.append({
                'id': t['id'], 'uri': t['uri'], 'name': t['name'],
                'artist': t['artists'][0]['name'] if t.get('artists') else 'Unknown',
                'all_artist_ids': [a['id'] for a in t.get('artists', []) if a.get('id')],
                'album': t['album']['name'] if t.get('album') else 'Unknown',
            })
        next_url = data.get('next')
        data = api_get_url(token, next_url) if next_url else None
    logger.info(f"Liked songs: {len(songs)}")
    return songs


def fetch_playlist_items_direct(token, playlist_id):
    songs = []
    data = api_get(token, f"/playlists/{playlist_id}/items", params={"limit": 50})
    while data:
        for entry in data.get('items', []):
            t = entry.get('item') or entry.get('track')
            if not t or not t.get('id'): continue
            songs.append({
                'id': t['id'],
                'uri': t.get('uri', f"spotify:track:{t['id']}"),
                'name': t.get('name', 'Unknown'),
                'artist': t['artists'][0]['name'] if t.get('artists') else 'Unknown',
                'all_artist_ids': [a['id'] for a in t.get('artists', []) if a.get('id')],
                'album': t['album']['name'] if t.get('album') else 'Unknown',
            })
        next_url = data.get('next')
        data = api_get_url(token, next_url) if next_url else None
    return songs


def fetch_all_playlist_songs_direct(token):
    all_songs, pl_meta = [], []
    data = api_get(token, "/me/playlists", params={"limit": 50})
    while data:
        for item in data.get('items', []):
            if not item: continue
            pid = item.get('id')
            pname = item.get('name', 'Untitled')
            items_info = item.get('items') or item.get('tracks') or {}
            tcount = items_info.get('total', 0)
            imgs = item.get('images', [])
            pl_meta.append({
                'id': pid, 'name': pname, 'tracks': tcount,
                'image': imgs[0]['url'] if imgs else None,
                'owner': item.get('owner', {}).get('display_name', ''),
                'description': item.get('description', ''),
            })
            if pid and tcount > 0:
                try:
                    pl_songs = fetch_playlist_items_direct(token, pid)
                    for s in pl_songs: s['source'] = f'playlist:{pname}'
                    all_songs.extend(pl_songs)
                except Exception as e:
                    logger.error(f"Items for '{pname}': {e}")
        next_url = data.get('next')
        data = api_get_url(token, next_url) if next_url else None
    logger.info(f"Playlists: {len(pl_meta)}, playlist songs: {len(all_songs)}")
    return all_songs, pl_meta


def fetch_top_artists_direct(token):
    artists = {}
    for tr in ['short_term', 'medium_term', 'long_term']:
        data = api_get(token, f"/me/top/artists", params={"limit": 50, "time_range": tr})
        if data:
            for i, a in enumerate(data.get('items', [])):
                if a.get('id') and a['id'] not in artists:
                    artists[a['id']] = {'id': a['id'], 'name': a['name'], 'genres': a.get('genres', []), 'rank': i}
    return list(artists.values())


def fetch_top_tracks_direct(token):
    tracks = []
    data = api_get(token, "/me/top/tracks", params={"limit": 50, "time_range": "medium_term"})
    if data:
        for t in data.get('items', []):
            if t and t.get('id'):
                tracks.append({'name': t['name'], 'artist': t['artists'][0]['name'] if t.get('artists') else 'Unknown'})
    return tracks


def deduplicate_songs(liked, playlist):
    seen, unique = set(), []
    for s in liked + playlist:
        if s['id'] not in seen: seen.add(s['id']); unique.append(s)
    return unique


# ─── Artist Genre Fetching ───

def fetch_artist_genres_batch(token, artist_ids, update_callback=None):
    """Fetch genres for artists. Tries batch endpoint first, falls back to
    individual fetches if batch fails.

    update_callback: optional function(message_string) to report progress
    """
    result = {}
    ids_list = list(artist_ids)
    if not ids_list:
        return result

    # === ATTEMPT 1: Batch endpoint GET /artists?ids= (up to 50 per request) ===
    logger.info(f"[GENRE] Trying batch artist endpoint for {len(ids_list)} artists...")
    batch_failed = False

    for i in range(0, len(ids_list), 50):
        batch = ids_list[i:i+50]
        ids_param = ",".join(batch)
        try:
            r = http_requests.get(
                f"{API_BASE}/artists",
                headers=make_auth_headers(token),
                params={"ids": ids_param},
                timeout=15
            )
            logger.info(f"[GENRE] Batch request [{i}:{i+len(batch)}]: status={r.status_code}")

            if r.status_code == 429:
                retry_after = int(r.headers.get('Retry-After', 5))
                logger.warning(f"[GENRE] Batch rate limited, Retry-After={retry_after}s")
                if retry_after <= 10:
                    time.sleep(retry_after)
                    r = http_requests.get(
                        f"{API_BASE}/artists",
                        headers=make_auth_headers(token),
                        params={"ids": ids_param},
                        timeout=15
                    )
                    logger.info(f"[GENRE] Batch retry: status={r.status_code}")
                else:
                    logger.warning(f"[GENRE] Batch blocked with long cooldown ({retry_after}s), switching to fallback")
                    batch_failed = True
                    break

            if r.status_code == 200:
                data = r.json()
                artists_data = data.get('artists', [])
                count = 0
                for artist in artists_data:
                    if artist and artist.get('id'):
                        result[artist['id']] = artist.get('genres', [])
                        count += 1
                logger.info(f"[GENRE] Batch chunk got {count} artists with genre data")
            elif r.status_code == 403:
                logger.error(f"[GENRE] Batch endpoint 403 Forbidden — may be restricted for dev mode apps. Body: {r.text[:300]}")
                batch_failed = True
                break
            else:
                logger.error(f"[GENRE] Batch returned {r.status_code}: {r.text[:300]}")
                batch_failed = True
                break
        except Exception as e:
            logger.error(f"[GENRE] Batch exception: {e}")
            batch_failed = True
            break

        if update_callback:
            update_callback(f"Fetching genres... {len(result)}/{len(ids_list)} artists")
        if i + 50 < len(ids_list):
            time.sleep(0.5)

    if not batch_failed:
        logger.info(f"[GENRE] Batch succeeded: {len(result)}/{len(ids_list)} artists")
        return result

    # === ATTEMPT 2: Individual endpoint GET /artists/{id} with careful pacing ===
    remaining = [aid for aid in ids_list if aid not in result]
    logger.info(f"[GENRE] Falling back to individual fetches for {len(remaining)} artists...")

    if update_callback:
        update_callback(f"Fetching genre data for {len(remaining)} artists (slow mode)...")

    for j, aid in enumerate(remaining):
        try:
            r = http_requests.get(
                f"{API_BASE}/artists/{aid}",
                headers=make_auth_headers(token),
                timeout=10
            )
            if j == 0:
                # Log the first response to see what Spotify gives us
                logger.info(f"[GENRE] First individual request: status={r.status_code}, headers={dict(r.headers)}")

            if r.status_code == 200:
                data = r.json()
                if data.get('id'):
                    result[data['id']] = data.get('genres', [])
            elif r.status_code == 429:
                retry_after = int(r.headers.get('Retry-After', 5))
                if retry_after <= 10:
                    logger.info(f"[GENRE] Individual rate limited, waiting {retry_after}s...")
                    time.sleep(retry_after)
                    r = http_requests.get(
                        f"{API_BASE}/artists/{aid}",
                        headers=make_auth_headers(token),
                        timeout=10
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if data.get('id'):
                            result[data['id']] = data.get('genres', [])
                else:
                    logger.warning(f"[GENRE] Individual rate limited with Retry-After={retry_after}s — stopping. Got {len(result)}/{len(ids_list)}")
                    break
            elif r.status_code == 403:
                logger.error(f"[GENRE] Individual artist 403 Forbidden — endpoint restricted. Body: {r.text[:200]}")
                break
            else:
                logger.error(f"[GENRE] Individual artist {aid}: status={r.status_code}")
        except Exception as e:
            logger.error(f"[GENRE] Individual artist {aid} exception: {e}")

        # Pace: 3 per second
        if (j + 1) % 3 == 0:
            time.sleep(1)

        if update_callback and (j + 1) % 20 == 0:
            update_callback(f"Fetching genres... {len(result)}/{len(ids_list)} artists done")

    logger.info(f"[GENRE] Final result: {len(result)}/{len(ids_list)} artists have genre data")
    return result


# ─── Background library loader ───

def load_library_background(user_id, token):
    """
    Runs in a background thread. Fetches the entire library, then
    fetches genres for ALL artists in bulk. No time pressure here.
    """
    try:
        loading_state[user_id] = {'status': 'loading', 'message': 'Fetching liked songs...'}

        liked = fetch_all_liked_songs_direct(token)
        loading_state[user_id]['message'] = f'Found {len(liked)} liked songs. Fetching playlists...'

        pl_songs, pl_meta = fetch_all_playlist_songs_direct(token)
        loading_state[user_id]['message'] = f'Found {len(pl_meta)} playlists. Processing...'

        top_a = fetch_top_artists_direct(token)
        top_t = fetch_top_tracks_direct(token)

        all_unique = deduplicate_songs(liked, pl_songs)
        summary = summarize_library(all_unique, pl_meta, top_a, top_t)

        library_cache[user_id] = summary
        full_library_cache[user_id] = all_unique
        playlists_cache[user_id] = pl_meta

        # Step 1: Seed genre cache with top artists (free — already fetched)
        genre_index = {}
        for a in top_a:
            aid = a.get('id')
            if aid and a.get('genres'):
                genre_index[aid] = a['genres']
        logger.info(f"[GENRE] Seeded with {len(genre_index)} top artists (free, no API calls)")

        # Step 2: Collect ALL unique artist IDs from the entire library
        all_artist_ids = set()
        for s in all_unique:
            all_artist_ids.update(s.get('all_artist_ids', []))

        # Step 3: Figure out which artists still need genre data
        need_fetch = all_artist_ids - set(genre_index.keys())
        logger.info(f"[GENRE] Library has {len(all_artist_ids)} unique artists, {len(genre_index)} already known, {len(need_fetch)} need fetching")

        # Step 4: Fetch remaining artist genres (background thread — no timeout)
        if need_fetch:
            loading_state[user_id]['message'] = f'Fetching genre data for {len(need_fetch)} artists...'

            def progress_update(msg):
                loading_state[user_id]['message'] = msg

            fetched = fetch_artist_genres_batch(token, list(need_fetch), update_callback=progress_update)
            genre_index.update(fetched)
            logger.info(f"[GENRE] After fetch: {len(genre_index)}/{len(all_artist_ids)} artists have genre data")

        artist_genre_index_cache[user_id] = genre_index

        loading_state[user_id] = {
            'status': 'ready',
            'total_songs': len(all_unique),
            'total_playlists': len(pl_meta),
        }
        logger.info(f"Library fully loaded for {user_id}: {len(all_unique)} songs, {len(pl_meta)} playlists, {len(genre_index)} artists with genres")

    except Exception as e:
        logger.error(f"Background load failed for {user_id}: {e}")
        loading_state[user_id] = {'status': 'error', 'message': str(e)}


# ─── Genre Analysis ───

GENRE_MAP = {
    'hip hop': 'Hip Hop', 'rap': 'Hip Hop', 'trap': 'Hip Hop',
    'r&b': 'R&B', 'soul': 'R&B', 'neo soul': 'R&B',
    'pop': 'Pop', 'dance pop': 'Pop', 'electropop': 'Pop',
    'rock': 'Rock', 'alternative': 'Rock', 'indie': 'Rock',
    'metal': 'Metal', 'hard rock': 'Metal',
    'country': 'Country', 'americana': 'Country',
    'electronic': 'Electronic', 'edm': 'Electronic', 'house': 'Electronic', 'techno': 'Electronic',
    'jazz': 'Jazz', 'folk': 'Folk', 'singer-songwriter': 'Folk',
    'reggae': 'Reggae', 'dancehall': 'Reggae',
    'latin': 'Latin', 'reggaeton': 'Latin',
    'punk': 'Punk', 'emo': 'Punk', 'blues': 'Blues',
    'funk': 'Funk', 'gospel': 'Gospel', 'classical': 'Classical',
}

def classify_genre(raw):
    low = raw.lower()
    if low in GENRE_MAP: return GENRE_MAP[low]
    for k, v in GENRE_MAP.items():
        if k in low: return v
    return 'Other'

def analyze_playlist_genres(token, playlist_id, user_id=None):
    """Analyze genres in a playlist using the pre-built genre cache.
    Genres were fetched during library load — this just looks them up.
    Only makes API calls to fetch playlist tracks (not artist data).
    """
    artist_ids = set()
    track_artists = []
    songs = fetch_playlist_items_direct(token, playlist_id)
    logger.info(f"analyze_playlist_genres: playlist {playlist_id} has {len(songs)} tracks")
    for s in songs:
        aids = s.get('all_artist_ids', [])
        artist_ids.update(aids)
        track_artists.append((s['id'], aids))
    logger.info(f"analyze_playlist_genres: {len(artist_ids)} unique artists in playlist")

    if not artist_ids:
        return {'main_genres': {}, 'sub_genres': {}, 'total_tracks': len(track_artists)}

    # Use the pre-built genre cache (populated during library load)
    ag = artist_genre_index_cache.get(user_id, {}) if user_id else {}
    cached_count = sum(1 for aid in artist_ids if aid in ag)
    uncached_count = len(artist_ids) - cached_count
    logger.info(f"analyze_playlist_genres: {cached_count}/{len(artist_ids)} artists have cached genres, {uncached_count} uncached")

    # If there are uncached artists, try fetching them on-demand as fallback
    if uncached_count > 0:
        need_fetch = [aid for aid in artist_ids if aid not in ag]
        logger.info(f"analyze_playlist_genres: attempting on-demand fetch for {len(need_fetch)} uncached artists")
        fetched = fetch_artist_genres_batch(token, need_fetch)
        if fetched:
            ag = dict(ag)  # copy so we don't mutate the original
            ag.update(fetched)
            # Save to cache for future use
            if user_id:
                if user_id not in artist_genre_index_cache:
                    artist_genre_index_cache[user_id] = {}
                artist_genre_index_cache[user_id].update(fetched)
            logger.info(f"analyze_playlist_genres: on-demand fetched {len(fetched)} additional artists")

    main_counts, sub_counts = Counter(), Counter()
    for _, aids in track_artists:
        for aid in aids:
            for g in ag.get(aid, []):
                sub_counts[g] += 1
                main_counts[classify_genre(g)] += 1
    grouped = defaultdict(list)
    for sg, c in sub_counts.most_common(60):
        grouped[classify_genre(sg)].append({'name': sg, 'count': c})

    total_tags = sum(main_counts.values())
    logger.info(f"analyze_playlist_genres: {len(main_counts)} main genres, {total_tags} total genre tags across tracks")
    return {'main_genres': dict(main_counts.most_common()), 'sub_genres': dict(grouped), 'total_tracks': len(track_artists)}

def build_artist_genre_index(token, all_songs):
    aids = set()
    for s in all_songs: aids.update(s.get('all_artist_ids', []))
    logger.info(f"Genre index: fetching {len(aids)} artists")
    return fetch_artist_genres_batch(token, list(aids))


# ─── Beef Up ───

def find_matching_songs(all_songs, selected_genres, existing_ids, genre_index):
    sel = set(g.lower() for g in selected_genres)
    existing = set(existing_ids)
    matches = []
    for s in all_songs:
        if s['id'] in existing: continue
        sg = set()
        for aid in s.get('all_artist_ids', []):
            for g in genre_index.get(aid, []): sg.add(g.lower())
        if sg & sel: matches.append(s)
    return matches

def get_playlist_track_ids(token, playlist_id):
    return [s['id'] for s in fetch_playlist_items_direct(token, playlist_id)]


# ─── Summarization (legacy) ───

def summarize_library(all_songs, pl_meta, top_artists, top_tracks):
    parts = [f"=== LIBRARY ({len(all_songs)} songs) ==="]
    if all_songs:
        by_artist = defaultdict(list)
        for s in all_songs: by_artist[s['artist']].append(s['name'])
        for a, songs in sorted(by_artist.items(), key=lambda x: len(x[1]), reverse=True)[:40]:
            sl = songs[:8]; extra = f" (+{len(songs)-8})" if len(songs) > 8 else ""
            parts.append(f"  - {a} ({len(songs)}): {', '.join(sl)}{extra}")
    if top_artists:
        parts.append(f"\nTop artists: {', '.join(a['name'] for a in sorted(top_artists, key=lambda a: a['rank'])[:25])}")
    if top_tracks:
        parts.append("\nTop tracks:")
        for t in top_tracks[:25]: parts.append(f"  - \"{t['name']}\" by {t['artist']}")
    return "\n".join(parts)


# ─── LLM (legacy) ───

def chat_with_llm(messages, system_prompt):
    if not LLM_API_KEY: return "AI not configured."
    try:
        resp = http_requests.post(f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
            json={"model": LLM_MODEL, "messages": [{"role": "system", "content": system_prompt}] + messages,
                  "max_tokens": 2000, "temperature": 0.7}, timeout=60)
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"LLM: {e}"); return "Sorry, something went wrong."

def _ensure_library_loaded(uid):
    if uid in library_cache and uid in full_library_cache: return True
    return False


# ─── Routes ───

@app.route('/')
def home():
    if is_authenticated(): return redirect(url_for('main_app'))
    return render_template('landing.html')

@app.route('/login')
def login():
    _, oauth, _ = get_spotify(); return redirect(oauth.get_authorize_url())

@app.route('/callback')
def callback():
    _, oauth, _ = get_spotify()
    code = request.args.get('code')
    if not code: return redirect(url_for('home'))
    oauth.get_access_token(code)
    return redirect(url_for('main_app'))

@app.route('/app')
def main_app():
    if not is_authenticated(): return redirect(url_for('home'))
    sp, _, _ = get_spotify(); u = sp.current_user()
    un = u.get('display_name') or u.get('id', 'Music Fan')
    av = u['images'][0]['url'] if u.get('images') and len(u['images']) > 0 else None
    return render_template('app.html', username=un, avatar=av)

@app.route('/classic')
def classic_page():
    if not is_authenticated(): return redirect(url_for('home'))
    sp, _, _ = get_spotify(); u = sp.current_user()
    un = u.get('display_name') or u.get('id', 'Music Fan')
    av = u['images'][0]['url'] if u.get('images') and len(u['images']) > 0 else None
    return render_template('classic.html', username=un, avatar=av)


# ─── API: Library Loading (async) ───

@app.route('/api/load-library')
def load_library():
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    token = get_access_token()
    if not token:
        return jsonify({'error': 'No valid token'}), 401

    me = api_get(token, "/me")
    if not me or not me.get('id'):
        return jsonify({'error': 'Could not identify user'}), 500
    user_id = me['id']
    session['spotify_user_id'] = user_id

    # Already loaded?
    if user_id in full_library_cache and user_id in library_cache:
        return jsonify({
            'status': 'ready',
            'total_songs': len(full_library_cache[user_id]),
            'total_playlists': len(playlists_cache.get(user_id, [])),
        })

    # Check if loading is in progress
    state = loading_state.get(user_id)
    if state:
        if state['status'] == 'loading':
            return jsonify({'status': 'loading', 'message': state.get('message', 'Loading...')})
        elif state['status'] == 'ready':
            return jsonify({
                'status': 'ready',
                'total_songs': state.get('total_songs', 0),
                'total_playlists': state.get('total_playlists', 0),
            })
        elif state['status'] == 'error':
            loading_state.pop(user_id, None)
            return jsonify({'status': 'error', 'message': state.get('message', 'Unknown error')})

    # Start background loading
    loading_state[user_id] = {'status': 'loading', 'message': 'Starting...'}
    thread = threading.Thread(target=load_library_background, args=(user_id, token), daemon=True)
    thread.start()

    return jsonify({'status': 'loading', 'message': 'Starting library scan...'})


# ─── API: Playlists ───

@app.route('/api/playlists')
def get_playlists():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    return jsonify({'playlists': playlists_cache.get(uid, [])})

@app.route('/api/analyze-playlist', methods=['POST'])
def analyze_playlist_route():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    pid = request.get_json().get('playlist_id')
    if not pid: return jsonify({'error': 'No playlist ID'}), 400
    token = get_access_token()
    uid = session.get('spotify_user_id', '')
    try:
        return jsonify(analyze_playlist_genres(token, pid, user_id=uid))
    except Exception as e:
        logger.error(f"Analyze: {e}"); return jsonify({'error': str(e)}), 500

@app.route('/api/beef-up', methods=['POST'])
def beef_up():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    all_songs = full_library_cache.get(uid, [])
    if not all_songs: return jsonify({'error': 'Library not loaded'}), 400
    data = request.get_json()
    pid, genres = data.get('playlist_id'), data.get('selected_genres', [])
    if not pid or not genres: return jsonify({'error': 'Missing data'}), 400
    token = get_access_token()
    if uid not in artist_genre_index_cache:
        artist_genre_index_cache[uid] = build_artist_genre_index(token, all_songs)
    existing = get_playlist_track_ids(token, pid)
    matches = find_matching_songs(all_songs, genres, existing, artist_genre_index_cache[uid])
    return jsonify({'success': True, 'added_count': len(matches),
                    'songs': [{'id': s['id'], 'name': s['name'], 'artist': s['artist']} for s in matches]})

@app.route('/api/confirm-beef-up', methods=['POST'])
def confirm_beef_up():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json()
    pid, sids = data.get('playlist_id'), data.get('song_ids', [])
    if not pid or not sids: return jsonify({'error': 'Missing data'}), 400
    token = get_access_token()
    try:
        uris = [f"spotify:track:{s}" for s in sids]
        for i in range(0, len(uris), 100):
            api_post(token, f"/playlists/{pid}/items", {"uris": uris[i:i+100]})
        return jsonify({'success': True, 'added_count': len(sids)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── API: Legacy Chat ───

@app.route('/api/chat', methods=['POST'])
def chat_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    if not library_cache.get(uid): return jsonify({'error': 'Library not loaded'}), 400
    return jsonify({'response': chat_with_llm(request.get_json().get('messages', [])[-20:],
        f"You are Playlist Buddy. Help write prompts for Spotify's AI.\n{library_cache[uid]}\nWrap in [PLAYLIST_PROMPT]...[/PLAYLIST_PROMPT].")})

@app.route('/api/creator-chat', methods=['POST'])
def creator_chat_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    if not full_library_cache.get(uid): return jsonify({'error': 'Library not loaded'}), 400
    songs = full_library_cache[uid]
    sl = "\n".join(f"{s['id']} | {s['name']} — {s['artist']}" for s in songs)
    return jsonify({'response': chat_with_llm(request.get_json().get('messages', [])[-20:],
        f"Playlist Creator. {len(songs)} songs.\n{sl}\nUse [PLAYLIST_SELECTION]playlist_name:...\\nsongs:\\nID[/PLAYLIST_SELECTION].")})

@app.route('/api/create-playlist', methods=['POST'])
def create_playlist_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json(); name = data.get('name', 'Playlist Buddy Mix')
    uid = session.get('spotify_user_id', '')
    valid = {s['id'] for s in full_library_cache.get(uid, [])}
    ids = [m.group() for r in data.get('song_ids', []) if (m := re.search(r'[0-9A-Za-z]{22}', r.strip())) and m.group() in valid]
    if not ids: return jsonify({'error': 'No valid songs'}), 400
    token = get_access_token()
    try:
        pl = api_post(token, "/me/playlists", {"name": name, "public": True, "description": "Created by Playlist Buddy"})
        uris = [f"spotify:track:{i}" for i in ids]
        for j in range(0, len(uris), 100): api_post(token, f"/playlists/{pl['id']}/items", {"uris": uris[j:j+100]})
        return jsonify({'success': True, 'playlist_url': pl['external_urls']['spotify'], 'playlist_name': name, 'track_count': len(ids)})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/song-details', methods=['POST'])
def song_details_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    lookup = {s['id']: s for s in full_library_cache.get(uid, [])}
    return jsonify({'songs': [{'id': s['id'], 'name': s['name'], 'artist': s['artist'], 'album': s['album']}
                              for sid in request.get_json().get('song_ids', []) if (s := lookup.get(sid))]})

@app.route('/logout')
def logout():
    uid = session.get('spotify_user_id', '')
    for c in [library_cache, full_library_cache, playlists_cache, artist_genre_index_cache, loading_state]: c.pop(uid, None)
    session.clear(); return redirect(url_for('home'))

if __name__ == "__main__":
    app.run(debug=True)
