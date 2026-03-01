"""
Playlist Buddy — "Beef Up" Your Playlists
==========================================
IMPORTANT: As of Feb 2026, many Spotipy methods are broken because
Spotify renamed/removed endpoints. This code uses direct HTTP calls
for anything that Spotipy can't handle:
  - GET /playlists/{id}/tracks → now /playlists/{id}/items
  - GET /artists (batch) → REMOVED, must fetch individually
  - POST /users/{id}/playlists → now POST /me/playlists
  - tracks field → items field in playlist responses
"""

import os, json, re, logging
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

library_cache = {}
full_library_cache = {}
playlists_cache = {}
artist_genre_index_cache = {}

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

def auth_headers():
    return {"Authorization": f"Bearer {get_access_token()}", "Content-Type": "application/json"}

def is_authenticated():
    ch = FlaskSessionCacheHandler(session)
    oauth = SpotifyOAuth(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                         redirect_uri=REDIRECT_URI, scope=SCOPE, cache_handler=ch)
    return oauth.validate_token(ch.get_cached_token()) is not None


# ─── Direct API helpers ───

def api_get(path, params=None):
    try:
        r = http_requests.get(f"{API_BASE}{path}", headers=auth_headers(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API GET {path}: {e}")
        return None

def api_get_url(url):
    """GET a full URL (for pagination 'next' links)."""
    try:
        r = http_requests.get(url, headers=auth_headers(), timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API GET {url}: {e}")
        return None

def api_post(path, data=None):
    r = http_requests.post(f"{API_BASE}{path}", headers=auth_headers(), json=data, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


# ─── Library Fetching ───

def fetch_all_liked_songs(sp):
    """Uses Spotipy — GET /me/tracks still works."""
    songs = []
    try:
        results = sp.current_user_saved_tracks(limit=50)
        while results:
            for item in results.get('items', []):
                t = item.get('track')
                if not t or not t.get('id'): continue
                songs.append({
                    'id': t['id'], 'uri': t['uri'], 'name': t['name'],
                    'artist': t['artists'][0]['name'] if t.get('artists') else 'Unknown',
                    'all_artist_ids': [a['id'] for a in t.get('artists', []) if a.get('id')],
                    'album': t['album']['name'] if t.get('album') else 'Unknown',
                })
            results = sp.next(results) if results.get('next') else None
    except Exception as e:
        logger.error(f"Liked songs error: {e}")
    logger.info(f"Liked songs: {len(songs)}")
    return songs


def fetch_playlist_items_direct(playlist_id):
    """
    GET /playlists/{id}/items (NEW endpoint, replaces /tracks)
    Response field: 'item' instead of 'track'
    """
    songs = []
    data = api_get(f"/playlists/{playlist_id}/items", params={"limit": 50})
    while data:
        for entry in data.get('items', []):
            t = entry.get('item') or entry.get('track')  # 'item' is new, 'track' is fallback
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
        data = api_get_url(next_url) if next_url else None
    return songs


def fetch_all_playlist_songs():
    """Fetch all playlists + their songs using direct API calls."""
    all_songs, pl_meta = [], []
    data = api_get("/me/playlists", params={"limit": 50})
    while data:
        for item in data.get('items', []):
            if not item: continue
            pid = item.get('id')
            pname = item.get('name', 'Untitled')
            # Feb 2026: 'tracks' → 'items' in playlist object
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
                    pl_songs = fetch_playlist_items_direct(pid)
                    for s in pl_songs: s['source'] = f'playlist:{pname}'
                    all_songs.extend(pl_songs)
                except Exception as e:
                    logger.error(f"Items for '{pname}': {e}")
        next_url = data.get('next')
        data = api_get_url(next_url) if next_url else None
    logger.info(f"Playlists: {len(pl_meta)}, songs: {len(all_songs)}")
    return all_songs, pl_meta


def fetch_top_artists(sp):
    artists = {}
    for tr in ['short_term', 'medium_term', 'long_term']:
        try:
            res = sp.current_user_top_artists(limit=50, time_range=tr)
            for i, a in enumerate(res.get('items', [])):
                if a.get('id') and a['id'] not in artists:
                    artists[a['id']] = {'name': a['name'], 'genres': a.get('genres', []), 'rank': i}
        except Exception as e:
            logger.error(f"Top artists ({tr}): {e}")
    return list(artists.values())

def fetch_top_tracks(sp):
    tracks = []
    try:
        res = sp.current_user_top_tracks(limit=50, time_range='medium_term')
        for t in res.get('items', []):
            if t and t.get('id'):
                tracks.append({'name': t['name'], 'artist': t['artists'][0]['name'] if t.get('artists') else 'Unknown'})
    except Exception as e:
        logger.error(f"Top tracks: {e}")
    return tracks

def deduplicate_songs(liked, playlist):
    seen, unique = set(), []
    for s in liked + playlist:
        if s['id'] not in seen: seen.add(s['id']); unique.append(s)
    return unique


# ─── Artist Genre Fetching (batch REMOVED, fetch individually) ───

def fetch_artist_genres_batch(artist_ids):
    """Fetch genres one artist at a time (GET /artists/{id})."""
    result = {}
    for aid in artist_ids:
        data = api_get(f"/artists/{aid}")
        if data and data.get('id'):
            result[data['id']] = data.get('genres', [])
    return result


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

def analyze_playlist_genres(playlist_id):
    artist_ids = set()
    track_artists = []
    songs = fetch_playlist_items_direct(playlist_id)
    for s in songs:
        aids = s.get('all_artist_ids', [])
        artist_ids.update(aids)
        track_artists.append((s['id'], aids))
    logger.info(f"Analyzing: {len(songs)} tracks, {len(artist_ids)} artists")
    ag = fetch_artist_genres_batch(list(artist_ids))
    main_counts, sub_counts = Counter(), Counter()
    for _, aids in track_artists:
        for aid in aids:
            for g in ag.get(aid, []):
                sub_counts[g] += 1
                main_counts[classify_genre(g)] += 1
    grouped = defaultdict(list)
    for sg, c in sub_counts.most_common(60):
        grouped[classify_genre(sg)].append({'name': sg, 'count': c})
    return {'main_genres': dict(main_counts.most_common()), 'sub_genres': dict(grouped), 'total_tracks': len(track_artists)}

def build_artist_genre_index(all_songs):
    aids = set()
    for s in all_songs: aids.update(s.get('all_artist_ids', []))
    logger.info(f"Genre index: fetching {len(aids)} artists individually")
    return fetch_artist_genres_batch(list(aids))


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

def get_playlist_track_ids(playlist_id):
    return [s['id'] for s in fetch_playlist_items_direct(playlist_id)]


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
    try:
        sp, _, _ = get_spotify()
        user = sp.current_user(); uid = user['id']
        liked = fetch_all_liked_songs(sp)
        pls, plm = fetch_all_playlist_songs()
        ta = fetch_top_artists(sp); tt = fetch_top_tracks(sp)
        uniq = deduplicate_songs(liked, pls)
        library_cache[uid] = summarize_library(uniq, plm, ta, tt)
        full_library_cache[uid] = uniq; playlists_cache[uid] = plm
        session['spotify_user_id'] = uid; return True
    except: return False


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


# ─── API ───

@app.route('/api/load-library')
def load_library():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    sp, _, _ = get_spotify()
    try:
        user = sp.current_user(); user_id = user['id']
    except Exception as e:
        logger.error(f"User ID: {e}"); return jsonify({'error': 'Could not identify user'}), 500

    if user_id in library_cache and user_id in full_library_cache:
        return jsonify({'status': 'ready', 'total_songs': len(full_library_cache[user_id]),
                        'total_playlists': len(playlists_cache.get(user_id, []))})

    try: liked = fetch_all_liked_songs(sp)
    except Exception as e: logger.error(f"Liked: {e}"); liked = []
    try: pl_songs, pl_meta = fetch_all_playlist_songs()
    except Exception as e: logger.error(f"Playlists: {e}"); pl_songs, pl_meta = [], []
    try: top_a = fetch_top_artists(sp)
    except Exception as e: logger.error(f"Top artists: {e}"); top_a = []
    try: top_t = fetch_top_tracks(sp)
    except Exception as e: logger.error(f"Top tracks: {e}"); top_t = []

    all_unique = deduplicate_songs(liked, pl_songs)
    library_cache[user_id] = summarize_library(all_unique, pl_meta, top_a, top_t)
    full_library_cache[user_id] = all_unique
    playlists_cache[user_id] = pl_meta
    session['spotify_user_id'] = user_id
    logger.info(f"Loaded: {len(all_unique)} songs, {len(pl_meta)} playlists for {user_id}")
    return jsonify({'status': 'ready', 'total_songs': len(all_unique), 'total_playlists': len(pl_meta)})

@app.route('/api/playlists')
def get_playlists():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    if not playlists_cache.get(uid) and not _ensure_library_loaded(uid):
        return jsonify({'error': 'Library not loaded'}), 400
    return jsonify({'playlists': playlists_cache.get(uid, [])})

@app.route('/api/analyze-playlist', methods=['POST'])
def analyze_playlist_route():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    pid = request.get_json().get('playlist_id')
    if not pid: return jsonify({'error': 'No playlist ID'}), 400
    try:
        a = analyze_playlist_genres(pid)
        return jsonify(a)
    except Exception as e:
        logger.error(f"Analyze: {e}"); return jsonify({'error': str(e)}), 500

@app.route('/api/beef-up', methods=['POST'])
def beef_up():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    all_songs = full_library_cache.get(uid, [])
    if not all_songs and not _ensure_library_loaded(uid):
        return jsonify({'error': 'Library not loaded'}), 400
    all_songs = full_library_cache.get(uid, [])
    data = request.get_json()
    pid, genres = data.get('playlist_id'), data.get('selected_genres', [])
    if not pid or not genres: return jsonify({'error': 'Missing data'}), 400
    if uid not in artist_genre_index_cache:
        artist_genre_index_cache[uid] = build_artist_genre_index(all_songs)
    existing = get_playlist_track_ids(pid)
    matches = find_matching_songs(all_songs, genres, existing, artist_genre_index_cache[uid])
    return jsonify({'success': True, 'added_count': len(matches),
                    'songs': [{'id': s['id'], 'name': s['name'], 'artist': s['artist']} for s in matches]})

@app.route('/api/confirm-beef-up', methods=['POST'])
def confirm_beef_up():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json()
    pid, sids = data.get('playlist_id'), data.get('song_ids', [])
    if not pid or not sids: return jsonify({'error': 'Missing data'}), 400
    try:
        uris = [f"spotify:track:{s}" for s in sids]
        for i in range(0, len(uris), 100):
            api_post(f"/playlists/{pid}/items", {"uris": uris[i:i+100]})
        return jsonify({'success': True, 'added_count': len(sids)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat', methods=['POST'])
def chat_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    if not library_cache.get(uid) and not _ensure_library_loaded(uid):
        return jsonify({'error': 'Library not loaded'}), 400
    return jsonify({'response': chat_with_llm(request.get_json().get('messages', [])[-20:],
        f"You are Playlist Buddy. Help write prompts for Spotify's AI.\n{library_cache[uid]}\nWrap in [PLAYLIST_PROMPT]...[/PLAYLIST_PROMPT].")})

@app.route('/api/creator-chat', methods=['POST'])
def creator_chat_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    if not full_library_cache.get(uid) and not _ensure_library_loaded(uid):
        return jsonify({'error': 'Library not loaded'}), 400
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
    try:
        pl = api_post("/me/playlists", {"name": name, "public": True, "description": "Created by Playlist Buddy"})
        uris = [f"spotify:track:{i}" for i in ids]
        for j in range(0, len(uris), 100): api_post(f"/playlists/{pl['id']}/items", {"uris": uris[j:j+100]})
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
    for c in [library_cache, full_library_cache, playlists_cache, artist_genre_index_cache]: c.pop(uid, None)
    session.clear(); return redirect(url_for('home'))

if __name__ == "__main__":
    app.run(debug=True)
