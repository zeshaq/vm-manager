from flask import Flask
# Import the blueprints
from views.listing import listing_bp
from views.creation import creation_bp
from views.storage import storage_bp  # <--- Import new blueprint

app = Flask(__name__)
app.secret_key = 'some_secret_key' # Required for flash messages if used

# Register the blueprints
app.register_blueprint(listing_bp)
app.register_blueprint(creation_bp)
app.register_blueprint(storage_bp)    # <--- Register new blueprint

# Simple route for the root URL
@app.route('/')
def index():
    from flask import render_template
    return render_template('home.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)