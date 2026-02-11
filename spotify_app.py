import os
from flask import Flask, session, request, redirect, url_for, render_template
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import FlaskSessionCacheHandler

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(64))

# Use environment variables for production
client_id = os.environ.get('SPOTIFY_CLIENT_ID', '6ea0978ad61247d7a2a8bbb0e417a943')
client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET', '660da264b0034a4499c4e3305dbd7f2e')
redirect_uri = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')
scope = 'playlist-read-private'

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
        return redirect(auth_url)
    return redirect(url_for('get_playlists'))

@app.route('/callback')
def callback():
    sp_oauth.get_access_token(request.args['code'])
    return redirect(url_for('get_playlists'))

@app.route('/playlists')
def get_playlists():
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
    
    # Paginate through all playlists
    while results['next']:
        results = sp.next(results)
        playlists.extend(results['items'])
    
    # Extract playlist info with images
    playlists_info = []
    for pl in playlists:
        playlists_info.append({
            'name': pl['name'],
            'url': pl['external_urls']['spotify'],
            'image': pl['images'][0]['url'] if pl['images'] else None,
            'tracks': pl['tracks']['total'],
            'public': pl['public']
        })
    
    return render_template('playlists.html', 
                         playlists=playlists_info, 
                         username=username,
                         total=len(playlists_info))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

if __name__ == "__main__":
    app.run(debug=True)
