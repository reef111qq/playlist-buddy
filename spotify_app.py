"""
Playlist Buddy — "Beef Up" Your Playlists
==========================================
Main workflow:
  1. User connects Spotify
  2. Sees grid of their playlists
  3. Picks one → app analyzes genres from track artists
  4. User selects which genres to add from
  5. App searches entire library for matching songs → adds to playlist

Legacy features (Prompt Builder + Playlist Creator) at /classic
"""

import os
import json
import re
import logging
from collections import Counter, defaultdict
from flask import Flask, session, request, redirect, url_for, render_template, jsonify
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(64))

# Server-side caches (keyed by Spotify user ID)
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


# === Spotify Auth Helpers ===

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
    if oauth.is_token_expired(ti):
        ti = oauth.refresh_access_token(ti['refresh_token'])
        ch.save_token_to_cache(ti)
    return ti['access_token']

def is_authenticated():
    ch = FlaskSessionCacheHandler(session)
    oauth = SpotifyOAuth(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                         redirect_uri=REDIRECT_URI, scope=SCOPE, cache_handler=ch)
    return oauth.validate_token(ch.get_cached_token()) is not None


# === Direct Spotify API (Feb 2026 endpoints) ===

def add_items_to_playlist_direct(token, playlist_id, uris):
    import requests as r
    for i in range(0, len(uris), 100):
        resp = r.post(f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"uris": uris[i:i+100]})
        resp.raise_for_status()

def create_playlist_direct(token, name, public=True, desc=""):
    import requests as r
    resp = r.post("https://api.spotify.com/v1/me/playlists",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                  json={"name": name, "public": public, "description": desc})
    resp.raise_for_status()
    return resp.json()


# === Library Fetching ===

def fetch_all_liked_songs(sp):
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
        logger.error(f"Error fetching liked songs: {e}")
    return songs

def fetch_all_playlist_songs(sp):
    all_songs, pl_meta = [], []
    try:
        results = sp.current_user_playlists(limit=50)
        while results:
            for item in results.get('items', []):
                if not item: continue
                pid = item.get('id')
                pname = item.get('name', 'Untitled')
                tcount = item.get('tracks', {}).get('total', 0)
                imgs = item.get('images', [])
                pl_meta.append({
                    'id': pid, 'name': pname, 'tracks': tcount,
                    'image': imgs[0]['url'] if imgs else None,
                    'owner': item.get('owner', {}).get('display_name', ''),
                    'description': item.get('description', ''),
                })
                if pid and tcount > 0:
                    try:
                        tr = sp.playlist_tracks(pid, fields='items(track(id,uri,name,artists(id,name),album(name))),next')
                        while tr:
                            for ti in tr.get('items', []):
                                t = ti.get('track')
                                if not t or not t.get('id'): continue
                                all_songs.append({
                                    'id': t['id'], 'uri': t['uri'], 'name': t['name'],
                                    'artist': t['artists'][0]['name'] if t.get('artists') else 'Unknown',
                                    'all_artist_ids': [a['id'] for a in t.get('artists', []) if a.get('id')],
                                    'album': t['album']['name'] if t.get('album') else 'Unknown',
                                    'source': f'playlist:{pname}',
                                })
                            tr = sp.next(tr) if tr.get('next') else None
                    except Exception as e:
                        logger.error(f"Error fetching tracks for '{pname}': {e}")
            results = sp.next(results) if results.get('next') else None
    except Exception as e:
        logger.error(f"Error fetching playlists: {e}")
    return all_songs, pl_meta

def fetch_top_artists(sp):
    artists = {}
    for tr in ['short_term', 'medium_term', 'long_term']:
        try:
            res = sp.current_user_top_artists(limit=50, time_range=tr)
            for i, a in enumerate(res.get('items', [])):
                if a.get('id') and a['id'] not in artists:
                    artists[a['id']] = {'name': a['name'], 'genres': a.get('genres', []), 'rank': i}
        except: pass
    return list(artists.values())

def fetch_top_tracks(sp):
    tracks = []
    try:
        res = sp.current_user_top_tracks(limit=50, time_range='medium_term')
        for t in res.get('items', []):
            if t and t.get('id'):
                tracks.append({'name': t['name'], 'artist': t['artists'][0]['name'] if t.get('artists') else 'Unknown'})
    except: pass
    return tracks

def deduplicate_songs(liked, playlist):
    seen, unique = set(), []
    for s in liked + playlist:
        if s['id'] not in seen:
            seen.add(s['id']); unique.append(s)
    return unique


# === Genre Analysis ===

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

def analyze_playlist_genres(sp, playlist_id):
    artist_ids = set()
    track_artists = []
    try:
        res = sp.playlist_tracks(playlist_id, fields='items(track(id,name,artists(id,name))),next')
        while res:
            for item in res.get('items', []):
                t = item.get('track')
                if not t or not t.get('id'): continue
                aids = [a['id'] for a in t.get('artists', []) if a.get('id')]
                artist_ids.update(aids)
                track_artists.append((t['id'], aids))
            res = sp.next(res) if res.get('next') else None
    except Exception as e:
        logger.error(f"Error analyzing playlist: {e}")
        return {'main_genres': {}, 'sub_genres': {}, 'total_tracks': 0}

    # Fetch artist genres in batches
    ag = {}
    for i in range(0, len(list(artist_ids)), 50):
        try:
            batch = list(artist_ids)[i:i+50]
            for a in sp.artists(batch).get('artists', []):
                if a and a.get('id'): ag[a['id']] = a.get('genres', [])
        except: pass

    main_counts, sub_counts = Counter(), Counter()
    for _, aids in track_artists:
        for aid in aids:
            for g in ag.get(aid, []):
                sub_counts[g] += 1
                main_counts[classify_genre(g)] += 1

    grouped = defaultdict(list)
    for sg, c in sub_counts.most_common(60):
        grouped[classify_genre(sg)].append({'name': sg, 'count': c})

    return {
        'main_genres': dict(main_counts.most_common()),
        'sub_genres': dict(grouped),
        'total_tracks': len(track_artists),
    }

def build_artist_genre_index(sp, all_songs):
    aids = set()
    for s in all_songs: aids.update(s.get('all_artist_ids', []))
    ag = {}
    aids_list = list(aids)
    for i in range(0, len(aids_list), 50):
        try:
            for a in sp.artists(aids_list[i:i+50]).get('artists', []):
                if a and a.get('id'): ag[a['id']] = a.get('genres', [])
        except: pass
    logger.info(f"Genre index: {len(ag)} artists")
    return ag


# === Beef Up Logic ===

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

def get_playlist_track_ids(sp, pid):
    ids = []
    try:
        res = sp.playlist_tracks(pid, fields='items(track(id)),next')
        while res:
            for item in res.get('items', []):
                t = item.get('track')
                if t and t.get('id'): ids.append(t['id'])
            res = sp.next(res) if res.get('next') else None
    except: pass
    return ids


# === Summarization (legacy) ===

def summarize_library(all_songs, pl_meta, top_artists, top_tracks):
    parts = [f"=== LIBRARY ({len(all_songs)} songs) ==="]
    if all_songs:
        by_artist = defaultdict(list)
        for s in all_songs: by_artist[s['artist']].append(s['name'])
        for a, songs in sorted(by_artist.items(), key=lambda x: len(x[1]), reverse=True)[:40]:
            sl = songs[:8]; extra = f" (+{len(songs)-8})" if len(songs) > 8 else ""
            parts.append(f"  - {a} ({len(songs)}): {', '.join(sl)}{extra}")
    if top_artists:
        sa = sorted(top_artists, key=lambda a: a['rank'])
        parts.append(f"\nTop artists: {', '.join(a['name'] for a in sa[:25])}")
    if top_tracks:
        parts.append("\nTop tracks:")
        for t in top_tracks[:25]: parts.append(f"  - \"{t['name']}\" by {t['artist']}")
    return "\n".join(parts)


# === LLM (legacy) ===

def chat_with_llm(messages, system_prompt):
    import requests as r
    if not LLM_API_KEY: return "AI not configured."
    try:
        resp = r.post(f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
            json={"model": LLM_MODEL, "messages": [{"role": "system", "content": system_prompt}] + messages,
                  "max_tokens": 2000, "temperature": 0.7}, timeout=60)
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"LLM error: {e}"); return "Sorry, something went wrong."

def _ensure_library_loaded(uid):
    if uid in library_cache and uid in full_library_cache: return True
    try:
        sp, _, _ = get_spotify()
        user = sp.current_user(); uid = user['id']
        liked = fetch_all_liked_songs(sp)
        pls, plm = fetch_all_playlist_songs(sp)
        ta = fetch_top_artists(sp); tt = fetch_top_tracks(sp)
        uniq = deduplicate_songs(liked, pls)
        library_cache[uid] = summarize_library(uniq, plm, ta, tt)
        full_library_cache[uid] = uniq; playlists_cache[uid] = plm
        session['spotify_user_id'] = uid; return True
    except: return False


# === Routes ===

@app.route('/')
def home():
    if is_authenticated(): return redirect(url_for('main_app'))
    return render_template('landing.html')

@app.route('/login')
def login():
    _, oauth, _ = get_spotify()
    return redirect(oauth.get_authorize_url())

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
    sp, _, _ = get_spotify()
    u = sp.current_user()
    un = u.get('display_name') or u.get('id', 'Music Fan')
    av = u['images'][0]['url'] if u.get('images') and len(u['images']) > 0 else None
    return render_template('app.html', username=un, avatar=av)

@app.route('/classic')
def classic_page():
    if not is_authenticated(): return redirect(url_for('home'))
    sp, _, _ = get_spotify()
    u = sp.current_user()
    un = u.get('display_name') or u.get('id', 'Music Fan')
    av = u['images'][0]['url'] if u.get('images') and len(u['images']) > 0 else None
    return render_template('classic.html', username=un, avatar=av)


# === API: Playlists ===

@app.route('/api/playlists')
def get_playlists():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    cached = playlists_cache.get(uid)
    if not cached:
        if not _ensure_library_loaded(uid): return jsonify({'error': 'Library not loaded'}), 400
        cached = playlists_cache.get(uid, [])
    return jsonify({'playlists': cached})

@app.route('/api/analyze-playlist', methods=['POST'])
def analyze_playlist():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    pid = request.get_json().get('playlist_id')
    if not pid: return jsonify({'error': 'No playlist ID'}), 400
    sp, _, _ = get_spotify()
    a = analyze_playlist_genres(sp, pid)
    return jsonify({'main_genres': a['main_genres'], 'sub_genres': a['sub_genres'], 'total_tracks': a['total_tracks']})

@app.route('/api/beef-up', methods=['POST'])
def beef_up():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    all_songs = full_library_cache.get(uid, [])
    if not all_songs:
        if not _ensure_library_loaded(uid): return jsonify({'error': 'Library not loaded'}), 400
        all_songs = full_library_cache.get(uid, [])
    data = request.get_json()
    pid = data.get('playlist_id'); genres = data.get('selected_genres', [])
    if not pid or not genres: return jsonify({'error': 'Missing data'}), 400
    sp, _, _ = get_spotify()
    if uid not in artist_genre_index_cache:
        artist_genre_index_cache[uid] = build_artist_genre_index(sp, all_songs)
    existing = get_playlist_track_ids(sp, pid)
    matches = find_matching_songs(all_songs, genres, existing, artist_genre_index_cache[uid])
    return jsonify({
        'success': True, 'added_count': len(matches),
        'songs': [{'id': s['id'], 'name': s['name'], 'artist': s['artist']} for s in matches],
    })

@app.route('/api/confirm-beef-up', methods=['POST'])
def confirm_beef_up():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json()
    pid = data.get('playlist_id'); sids = data.get('song_ids', [])
    if not pid or not sids: return jsonify({'error': 'Missing data'}), 400
    try:
        add_items_to_playlist_direct(get_access_token(), pid, [f"spotify:track:{s}" for s in sids])
        return jsonify({'success': True, 'added_count': len(sids)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# === API: Legacy ===

@app.route('/api/chat', methods=['POST'])
def chat_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    if not library_cache.get(uid) and not _ensure_library_loaded(uid):
        return jsonify({'error': 'Library not loaded'}), 400
    msgs = request.get_json().get('messages', [])[-20:]
    return jsonify({'response': chat_with_llm(msgs, f"""You are Playlist Buddy. Help write prompts for Spotify's AI playlist feature.
{library_cache[uid]}
Wrap prompts in [PLAYLIST_PROMPT]...[/PLAYLIST_PROMPT] tags. Be creative, reference their library.""")})

@app.route('/api/creator-chat', methods=['POST'])
def creator_chat_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    uid = session.get('spotify_user_id', '')
    if not full_library_cache.get(uid) and not _ensure_library_loaded(uid):
        return jsonify({'error': 'Library not loaded'}), 400
    msgs = request.get_json().get('messages', [])[-20:]
    songs = full_library_cache[uid]
    sl = "\n".join(f"{s['id']} | {s['name']} — {s['artist']}" for s in songs)
    return jsonify({'response': chat_with_llm(msgs, f"""Playlist Creator mode. {len(songs)} songs.
{sl}
Select songs in [PLAYLIST_SELECTION]playlist_name:...\\nsongs:\\nID1\\nID2[/PLAYLIST_SELECTION] format.""")})

@app.route('/api/create-playlist', methods=['POST'])
def create_playlist_api():
    if not is_authenticated(): return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json()
    name = data.get('name', 'Playlist Buddy Mix')
    raw = data.get('song_ids', [])
    uid = session.get('spotify_user_id', '')
    valid = {s['id'] for s in full_library_cache.get(uid, [])}
    ids = [m.group() for r in raw if (m := re.search(r'[0-9A-Za-z]{22}', r.strip())) and m.group() in valid]
    if not ids: return jsonify({'error': 'No valid songs'}), 400
    try:
        tok = get_access_token()
        pl = create_playlist_direct(tok, name, True, "Created by Playlist Buddy")
        add_items_to_playlist_direct(tok, pl['id'], [f"spotify:track:{i}" for i in ids])
        return jsonify({'success': True, 'playlist_url': pl['external_urls']['spotify'], 'playlist_name': name, 'track_count': len(ids)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
    for c in [library_cache, full_library_cache, playlists_cache, artist_genre_index_cache]:
        c.pop(uid, None)
    session.clear()
    return redirect(url_for('home'))

if __name__ == "__main__":
    app.run(debug=True)
