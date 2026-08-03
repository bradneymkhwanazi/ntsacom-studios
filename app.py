import os
import uuid
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_from_directory, abort, session, g
)
from werkzeug.utils import secure_filename
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# ─── Configuration (env vars for production) ────────────────────────────────────

app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# Use persistent disk on Render, local otherwise
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
THUMBNAIL_FOLDER = os.path.join(UPLOAD_FOLDER, 'thumbnails')
DB_PATH = os.path.join(DATA_DIR, 'gallery.db')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'tiff'}
PACKAGES = {'unlimited': None, '50': 50, '20': 20}

# Admin credentials
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme123')

# Email config (optional — leave empty to disable)
SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM', '')

# Watermark text
WATERMARK_TEXT = os.environ.get('WATERMARK_TEXT', 'Ntsacom Studios')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max per file

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)


# ─── Database ───────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS galleries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            package TEXT NOT NULL DEFAULT 'unlimited',
            download_limit INTEGER,
            customer_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gallery_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (gallery_id) REFERENCES galleries(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gallery_id INTEGER NOT NULL,
            photo_id INTEGER NOT NULL,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (gallery_id) REFERENCES galleries(id),
            FOREIGN KEY (photo_id) REFERENCES photos(id)
        );
    ''')
    db.commit()


with app.app_context():
    init_db()


# ─── Helpers ────────────────────────────────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def create_thumbnail(filepath, filename):
    """Create a watermarked thumbnail for gallery preview."""
    thumb_path = os.path.join(THUMBNAIL_FOLDER, filename)
    with Image.open(filepath) as img:
        img.thumbnail((600, 600))

        # Add watermark
        if WATERMARK_TEXT:
            draw = ImageDraw.Draw(img)
            width, height = img.size

            # Calculate font size relative to image
            font_size = max(20, min(width, height) // 8)
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
            except (OSError, IOError):
                try:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
                except (OSError, IOError):
                    font = ImageFont.load_default()

            # Get text bounding box
            bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]

            # Center the watermark
            x = (width - text_width) // 2
            y = (height - text_height) // 2

            # Draw semi-transparent watermark with shadow
            # Shadow
            draw.text((x + 2, y + 2), WATERMARK_TEXT, font=font, fill=(0, 0, 0, 80))
            # Main text
            draw.text((x, y), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 100))

        img.save(thumb_path, quality=85)
    return thumb_path


def get_download_count(gallery_id):
    """Count unique photos downloaded for a gallery."""
    db = get_db()
    result = db.execute(
        'SELECT COUNT(DISTINCT photo_id) as count FROM downloads WHERE gallery_id = ?',
        (gallery_id,)
    ).fetchone()
    return result['count']


def send_gallery_email(to_email, gallery_name, share_url):
    """Send gallery link to customer via email."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, to_email]):
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'Your photos are ready — {gallery_name} | Ntsacom Studios'
    msg['From'] = EMAIL_FROM or SMTP_USER
    msg['To'] = to_email

    text_body = f"""Hi there!

Your photos from "{gallery_name}" are ready for viewing and download.

View your gallery here:
{share_url}

Simply click the link above to browse your photos and download your favourites.

Thank you for choosing Ntsacom Studios!
"""

    html_body = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="text-align: center; padding: 30px 0;">
            <h1 style="color: #333; font-size: 24px;">📷 Ntsacom Studios</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.6;">
                Your photos from <strong>{gallery_name}</strong> are ready for viewing and download.
            </p>
            <a href="{share_url}"
               style="display: inline-block; background: #333; color: #fff; padding: 14px 30px;
                      border-radius: 6px; text-decoration: none; font-size: 16px; margin: 20px 0;">
                View Your Gallery
            </a>
            <p style="color: #999; font-size: 13px; margin-top: 30px;">
                Simply click the button above to browse and download your photos.
            </p>
        </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(text_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


# ─── Admin Routes ───────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials', 'error')
    return render_template('admin/login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@login_required
def admin_dashboard():
    db = get_db()
    galleries = db.execute(
        'SELECT *, (SELECT COUNT(*) FROM photos WHERE gallery_id = galleries.id) as photo_count '
        'FROM galleries ORDER BY created_at DESC'
    ).fetchall()
    return render_template('admin/dashboard.html', galleries=galleries)


@app.route('/admin/gallery/new', methods=['GET', 'POST'])
@login_required
def create_gallery():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        package = request.form.get('package', 'unlimited')
        customer_email = request.form.get('customer_email', '').strip()

        if not name:
            flash('Gallery name is required', 'error')
            return render_template('admin/create_gallery.html', packages=PACKAGES)

        if package not in PACKAGES:
            flash('Invalid package selected', 'error')
            return render_template('admin/create_gallery.html', packages=PACKAGES)

        token = uuid.uuid4().hex[:12]
        download_limit = PACKAGES[package]

        db = get_db()
        db.execute(
            'INSERT INTO galleries (name, token, package, download_limit, customer_email) VALUES (?, ?, ?, ?, ?)',
            (name, token, package, download_limit, customer_email or None)
        )
        db.commit()

        flash(f'Gallery "{name}" created successfully!', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/create_gallery.html', packages=PACKAGES)


@app.route('/admin/gallery/<int:gallery_id>')
@login_required
def admin_gallery_detail(gallery_id):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE id = ?', (gallery_id,)).fetchone()
    if not gallery:
        abort(404)

    photos = db.execute(
        'SELECT * FROM photos WHERE gallery_id = ? ORDER BY uploaded_at DESC', (gallery_id,)
    ).fetchall()

    download_count = get_download_count(gallery_id)
    share_url = request.host_url.rstrip('/') + '/gallery/' + gallery['token']

    return render_template(
        'admin/gallery_detail.html',
        gallery=gallery, photos=photos,
        download_count=download_count, share_url=share_url
    )


@app.route('/admin/gallery/<int:gallery_id>/upload', methods=['POST'])
@login_required
def upload_photos(gallery_id):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE id = ?', (gallery_id,)).fetchone()
    if not gallery:
        abort(404)

    files = request.files.getlist('photos')
    uploaded = 0

    for file in files:
        if file and allowed_file(file.filename):
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

            file.save(filepath)
            create_thumbnail(filepath, filename)

            db.execute(
                'INSERT INTO photos (gallery_id, filename, original_name) VALUES (?, ?, ?)',
                (gallery_id, filename, file.filename)
            )
            uploaded += 1

    db.commit()
    flash(f'{uploaded} photo(s) uploaded successfully!', 'success')
    return redirect(url_for('admin_gallery_detail', gallery_id=gallery_id))


@app.route('/admin/gallery/<int:gallery_id>/send-email', methods=['POST'])
@login_required
def send_email_to_customer(gallery_id):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE id = ?', (gallery_id,)).fetchone()
    if not gallery:
        abort(404)

    email = gallery['customer_email']
    if not email:
        flash('No customer email set for this gallery.', 'error')
        return redirect(url_for('admin_gallery_detail', gallery_id=gallery_id))

    share_url = request.host_url.rstrip('/') + '/gallery/' + gallery['token']
    success = send_gallery_email(email, gallery['name'], share_url)

    if success:
        flash(f'Email sent to {email}!', 'success')
    else:
        if not SMTP_HOST:
            flash('Email not configured. Set SMTP_HOST, SMTP_USER, and SMTP_PASSWORD environment variables.', 'error')
        else:
            flash('Failed to send email. Check your SMTP settings.', 'error')

    return redirect(url_for('admin_gallery_detail', gallery_id=gallery_id))


@app.route('/admin/gallery/<int:gallery_id>/delete', methods=['POST'])
@login_required
def delete_gallery(gallery_id):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE id = ?', (gallery_id,)).fetchone()
    if not gallery:
        abort(404)

    photos = db.execute('SELECT filename FROM photos WHERE gallery_id = ?', (gallery_id,)).fetchall()
    for photo in photos:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo['filename'])
        thumb_path = os.path.join(THUMBNAIL_FOLDER, photo['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)

    db.execute('DELETE FROM downloads WHERE gallery_id = ?', (gallery_id,))
    db.execute('DELETE FROM photos WHERE gallery_id = ?', (gallery_id,))
    db.execute('DELETE FROM galleries WHERE id = ?', (gallery_id,))
    db.commit()

    flash('Gallery deleted', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/photo/<int:photo_id>/delete', methods=['POST'])
@login_required
def delete_photo(photo_id):
    db = get_db()
    photo = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    if not photo:
        abort(404)

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo['filename'])
    thumb_path = os.path.join(THUMBNAIL_FOLDER, photo['filename'])
    if os.path.exists(filepath):
        os.remove(filepath)
    if os.path.exists(thumb_path):
        os.remove(thumb_path)

    db.execute('DELETE FROM downloads WHERE photo_id = ?', (photo_id,))
    db.execute('DELETE FROM photos WHERE id = ?', (photo_id,))
    db.commit()

    flash('Photo deleted', 'success')
    return redirect(url_for('admin_gallery_detail', gallery_id=photo['gallery_id']))


# ─── Customer Routes ────────────────────────────────────────────────────────────

@app.route('/gallery/<token>')
def customer_gallery(token):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
    if not gallery:
        abort(404)

    photos = db.execute(
        'SELECT * FROM photos WHERE gallery_id = ? ORDER BY uploaded_at', (gallery['id'],)
    ).fetchall()

    download_count = get_download_count(gallery['id'])
    downloads_remaining = None
    if gallery['download_limit'] is not None:
        downloads_remaining = max(0, gallery['download_limit'] - download_count)

    downloaded_ids = [row['photo_id'] for row in db.execute(
        'SELECT DISTINCT photo_id FROM downloads WHERE gallery_id = ?', (gallery['id'],)
    ).fetchall()]

    return render_template(
        'customer/gallery.html',
        gallery=gallery, photos=photos,
        downloads_remaining=downloads_remaining,
        downloaded_ids=downloaded_ids
    )


@app.route('/gallery/<token>/download/<int:photo_id>')
def download_photo(token, photo_id):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
    if not gallery:
        abort(404)

    photo = db.execute(
        'SELECT * FROM photos WHERE id = ? AND gallery_id = ?', (photo_id, gallery['id'])
    ).fetchone()
    if not photo:
        abort(404)

    # Check if already downloaded (re-downloads are free)
    already_downloaded = db.execute(
        'SELECT id FROM downloads WHERE gallery_id = ? AND photo_id = ?',
        (gallery['id'], photo_id)
    ).fetchone()

    if not already_downloaded:
        if gallery['download_limit'] is not None:
            download_count = get_download_count(gallery['id'])
            if download_count >= gallery['download_limit']:
                flash('Download limit reached for this gallery.', 'error')
                return redirect(url_for('customer_gallery', token=token))

        db.execute(
            'INSERT INTO downloads (gallery_id, photo_id) VALUES (?, ?)',
            (gallery['id'], photo_id)
        )
        db.commit()

    return send_from_directory(
        app.config['UPLOAD_FOLDER'],
        photo['filename'],
        download_name=photo['original_name'],
        as_attachment=True
    )


# ─── Static file serving ────────────────────────────────────────────────────────

@app.route('/uploads/thumbnails/<filename>')
def serve_thumbnail(filename):
    return send_from_directory(THUMBNAIL_FOLDER, filename)


# ─── Home redirect ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('admin_login'))


if __name__ == '__main__':
    app.run(debug=True, port=5001)
