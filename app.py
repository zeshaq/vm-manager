from flask import Flask, session, redirect, url_for, request, render_template
import simplepam
import os

# Import the blueprints
from views.listing import listing_bp
from views.creation import creation_bp
from views.storage import storage_bp  # <--- Import new blueprint

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Register the blueprints
app.register_blueprint(listing_bp)
app.register_blueprint(creation_bp)
app.register_blueprint(storage_bp)    # <--- Register new blueprint

@app.before_request
def before_request():
    if 'username' not in session and request.endpoint not in ['login']:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if simplepam.authenticate(username, password):
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

# Simple route for the root URL
@app.route('/')
def index():
    return render_template('home.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)