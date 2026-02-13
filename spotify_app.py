import os
from flask import Flask, session, request, redirect, url_for, render_template, jsonify
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(64))

# Use environment variables for production
client_id = os.environ.get('SPOTIFY_CLIENT_ID', '6ea0978ad61247d7a2a8bbb0e417a943')
client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET', '660da264b0034a4499c4e3305dbd7f2e')
redirect_uri = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')
scope = 'user-library-read playlist-read-private'

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

@app.route('/')
def home():
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        auth_url = sp_oauth.get_authorize_url()
        return render_template('login.html', auth_url=auth_url)
    return redirect(url_for('finder'))

@app.route('/callback')
def callback():
    sp_oauth.get_access_token(request.args['code'])
    return redirect(url_for('finder'))

@app.route('/finder')
def finder():
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        auth_url = sp_oauth.get_authorize_url()
        return redirect(auth_url)

    # Get user info
    user = sp.current_user()
    username = user['display_name'] or user['id']
    
    return render_template('finder.html', username=username)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/api/search')
def search_songs():
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401
    
    sp = Spotify(auth_manager=sp_oauth)
    query = request.args.get('q', '')
    
    if not query:
        return jsonify({'results': []})
    
    # Search in user's saved tracks
    results = sp.current_user_saved_tracks(limit=50)
    tracks = results['items']
    
    # Simple search - filter by song name or artist
    matching = []
    for item in tracks:
        track = item['track']
        track_name = track['name'].lower()
        artist_name = track['artists'][0]['name'].lower()
        
        if query.lower() in track_name or query.lower() in artist_name:
            matching.append({
                'id': track['id'],
                'name': track['name'],
                'artist': track['artists'][0]['name'],
                'image': track['album']['images'][0]['url'] if track['album']['images'] else None,
            })
    
    return jsonify({'results': matching[:10]})

@app.route('/api/similar/<track_id>')
def find_similar(track_id):
    if not sp_oauth.validate_token(cache_handler.get_cached_token()):
        return jsonify({'error': 'Not authenticated'}), 401
    
    sp = Spotify(auth_manager=sp_oauth)
    
    # TODO: Implement actual similarity algorithm
    # For now, return random tracks from library as placeholder
    import time
    time.sleep(1)  # Simulate processing
    
    results = sp.current_user_saved_tracks(limit=20)
    similar_tracks = []
    
    for item in results['items'][:10]:
        track = item['track']
        similar_tracks.append({
            'id': track['id'],
            'name': track['name'],
            'artist': track['artists'][0]['name'],
            'image': track['album']['images'][0]['url'] if track['album']['images'] else None,
            'similarity_score': 0.85  # Mock similarity score
        })
    
    return jsonify({'similar_tracks': similar_tracks})

if __name__ == "__main__":
    app.run(debug=True)
