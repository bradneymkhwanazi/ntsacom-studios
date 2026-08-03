import os
import uuid
import sqlite3
import smtplib
import zipfile
import io
import json
import urllib.parse
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_from_directory, send_file, abort, session, g, jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256')

from PIL import Image, ImageDraw, ImageFont
from drive_storage import (
    is_drive_enabled, upload_to_drive, download_from_drive,
    delete_from_drive, make_file_public, get_drive_download_url
)

app = Flask(__name__)

# ─── Configuration ──────────────────────────────────────────────────────────────

app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
THUMBNAIL_FOLDER = os.path.join(UPLOAD_FOLDER, 'thumbnails')
DB_PATH = os.path.join(DATA_DIR, 'gallery.db')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'tiff'}
PACKAGES = {'unlimited': None, '50': 50, '20': 20}
EXPIRY_OPTIONS = {'never': None, '30': 30, '60': 60, '90': 90}

SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM', '')

WATERMARK_TEXT = os.environ.get('WATERMARK_TEXT', 'Ntsacom Studios')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

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
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS galleries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            package TEXT NOT NULL DEFAULT 'unlimited',
            download_limit INTEGER,
            customer_email TEXT,
            password TEXT,
            expires_at TIMESTAMP,
            requires_payment INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gallery_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            drive_file_id TEXT,
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

        CREATE TABLE IF NOT EXISTS favourites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gallery_id INTEGER NOT NULL,
            photo_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (gallery_id) REFERENCES galleries(id),
            FOREIGN KEY (photo_id) REFERENCES photos(id)
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gallery_id INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            currency TEXT DEFAULT 'ZAR',
            status TEXT DEFAULT 'pending',
            paid_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (gallery_id) REFERENCES galleries(id)
        );
    ''')
    db.commit()


with app.app_context():
    init_db()


# ─── Helpers ────────────────────────────────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def is_gallery_expired(gallery):
    if not gallery['expires_at']:
        return False
    expires = datetime.strptime(gallery['expires_at'], '%Y-%m-%d %H:%M:%S')
    return datetime.now() > expires


def is_gallery_paid(gallery):
    if not gallery['requires_payment']:
        return True
    db = get_db()
    invoice = db.execute(
        'SELECT * FROM invoices WHERE gallery_id = ? AND status = ?',
        (gallery['id'], 'paid')
    ).fetchone()
    return invoice is not None


def create_thumbnail(filepath, filename):
    thumb_path = os.path.join(THUMBNAIL_FOLDER, filename)
    with Image.open(filepath) as img:
        img.thumbnail((600, 600))
        if WATERMARK_TEXT:
            draw = ImageDraw.Draw(img)
            width, height = img.size
            font_size = max(20, min(width, height) // 8)
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
            except (OSError, IOError):
                try:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
                except (OSError, IOError):
                    font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x = (width - text_width) // 2
            y = (height - text_height) // 2
            draw.text((x + 2, y + 2), WATERMARK_TEXT, font=font, fill=(0, 0, 0, 80))
            draw.text((x, y), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 100))
        img.save(thumb_path, quality=85)
    return thumb_path


def get_download_count(gallery_id):
    db = get_db()
    result = db.execute(
        'SELECT COUNT(DISTINCT photo_id) as count FROM downloads WHERE gallery_id = ?',
        (gallery_id,)
    ).fetchone()
    return result['count']


def send_gallery_email(to_email, gallery_name, share_url):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, to_email]):
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'Your photos are ready — {gallery_name} | Ntsacom Studios'
    msg['From'] = EMAIL_FROM or SMTP_USER
    msg['To'] = to_email
    text_body = f'Hi there!\n\nYour photos from "{gallery_name}" are ready.\n\nView here: {share_url}\n\nThank you for choosing Ntsacom Studios!'
    html_body = f'''<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;text-align:center;">
    <h1>📷 Ntsacom Studios</h1>
    <p>Your photos from <strong>{gallery_name}</strong> are ready!</p>
    <a href="{share_url}" style="display:inline-block;background:#333;color:#fff;padding:14px 30px;border-radius:6px;text-decoration:none;margin:20px 0;">View Your Gallery</a>
    </body></html>'''
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
        if not session.get('user_id'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


def get_whatsapp_link(share_url, gallery_name):
    text = f"Hi! Your photos from '{gallery_name}' are ready. View and download them here: {share_url}"
    return f"https://wa.me/?text={urllib.parse.quote(text)}"


# ─── Admin Auth Routes ──────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    db = get_db()
    user_count = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
    if user_count == 0:
        return redirect(url_for('admin_setup'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials', 'error')
    return render_template('admin/login.html')


@app.route('/admin/setup', methods=['GET', 'POST'])
def admin_setup():
    db = get_db()
    user_count = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
    if user_count > 0:
        return redirect(url_for('admin_login'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password required', 'error')
        elif len(password) < 6:
            flash('Password must be at least 6 characters', 'error')
        else:
            db.execute(
                'INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)',
                (username, hash_password(password))
            )
            db.commit()
            flash('Admin account created! Please log in.', 'success')
            return redirect(url_for('admin_login'))
    return render_template('admin/setup.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


# ─── Admin User Management ──────────────────────────────────────────────────────

@app.route('/admin/users')
@login_required
def admin_users():
    db = get_db()
    users = db.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    return render_template('admin/users.html', users=users)


@app.route('/admin/users/new', methods=['POST'])
@login_required
def create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if not username or not password:
        flash('Username and password required', 'error')
    elif len(password) < 6:
        flash('Password must be at least 6 characters', 'error')
    else:
        db = get_db()
        try:
            db.execute(
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                (username, hash_password(password))
            )
            db.commit()
            flash(f'User "{username}" created', 'success')
        except sqlite3.IntegrityError:
            flash('Username already exists', 'error')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user(user_id):
    db = get_db()
    if user_id == session.get('user_id'):
        flash("You can't delete your own account", 'error')
    else:
        db.execute('DELETE FROM users WHERE id = ?', (user_id,))
        db.commit()
        flash('User deleted', 'success')
    return redirect(url_for('admin_users'))


# ─── Admin Dashboard & Analytics ────────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_dashboard():
    db = get_db()
    galleries = db.execute(
        'SELECT *, (SELECT COUNT(*) FROM photos WHERE gallery_id = galleries.id) as photo_count '
        'FROM galleries ORDER BY created_at DESC'
    ).fetchall()
    return render_template('admin/dashboard.html', galleries=galleries)


@app.route('/admin/analytics')
@login_required
def admin_analytics():
    db = get_db()
    stats = {
        'total_galleries': db.execute('SELECT COUNT(*) as c FROM galleries').fetchone()['c'],
        'total_photos': db.execute('SELECT COUNT(*) as c FROM photos').fetchone()['c'],
        'total_downloads': db.execute('SELECT COUNT(*) as c FROM downloads').fetchone()['c'],
    }
    top_photos = db.execute('''
        SELECT p.original_name, p.filename, g.name as gallery_name, COUNT(d.id) as download_count
        FROM downloads d
        JOIN photos p ON d.photo_id = p.id
        JOIN galleries g ON d.gallery_id = g.id
        GROUP BY d.photo_id ORDER BY download_count DESC LIMIT 10
    ''').fetchall()
    daily_downloads = db.execute('''
        SELECT DATE(downloaded_at) as day, COUNT(*) as count
        FROM downloads
        WHERE downloaded_at >= DATE('now', '-30 days')
        GROUP BY day ORDER BY day
    ''').fetchall()
    active_galleries = db.execute('''
        SELECT g.name, g.id, COUNT(d.id) as download_count
        FROM galleries g
        JOIN downloads d ON d.gallery_id = g.id
        GROUP BY g.id ORDER BY download_count DESC LIMIT 10
    ''').fetchall()
    return render_template('admin/analytics.html',
        stats=stats, top_photos=top_photos,
        daily_downloads=json.dumps([{'day': r['day'], 'count': r['count']} for r in daily_downloads]),
        active_galleries=active_galleries
    )


# ─── Admin Gallery CRUD ─────────────────────────────────────────────────────────

@app.route('/admin/gallery/new', methods=['GET', 'POST'])
@login_required
def create_gallery():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        package = request.form.get('package', 'unlimited')
        customer_email = request.form.get('customer_email', '').strip()
        expiry = request.form.get('expiry', 'never')
        password = request.form.get('password', '').strip()
        requires_payment = request.form.get('requires_payment') == 'on'
        amount = request.form.get('amount', '0').strip()

        if not name:
            flash('Gallery name is required', 'error')
            return render_template('admin/create_gallery.html', packages=PACKAGES, expiry_options=EXPIRY_OPTIONS)
        if package not in PACKAGES:
            flash('Invalid package', 'error')
            return render_template('admin/create_gallery.html', packages=PACKAGES, expiry_options=EXPIRY_OPTIONS)

        token = uuid.uuid4().hex[:12]
        download_limit = PACKAGES[package]
        expires_at = None
        if expiry in EXPIRY_OPTIONS and EXPIRY_OPTIONS[expiry]:
            expires_at = (datetime.now() + timedelta(days=EXPIRY_OPTIONS[expiry])).strftime('%Y-%m-%d %H:%M:%S')

        db = get_db()
        db.execute(
            '''INSERT INTO galleries (name, token, package, download_limit, customer_email, password, expires_at, requires_payment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (name, token, package, download_limit, customer_email or None,
             password or None, expires_at, 1 if requires_payment else 0)
        )
        db.commit()

        gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
        if requires_payment and amount:
            try:
                db.execute(
                    'INSERT INTO invoices (gallery_id, amount) VALUES (?, ?)',
                    (gallery['id'], float(amount))
                )
                db.commit()
            except ValueError:
                pass

        flash(f'Gallery "{name}" created!', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/create_gallery.html', packages=PACKAGES, expiry_options=EXPIRY_OPTIONS)


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
    whatsapp_link = get_whatsapp_link(share_url, gallery['name'])
    invoice = db.execute('SELECT * FROM invoices WHERE gallery_id = ? ORDER BY created_at DESC LIMIT 1', (gallery_id,)).fetchone()
    favourites = db.execute(
        'SELECT photo_id, COUNT(*) as count FROM favourites WHERE gallery_id = ? GROUP BY photo_id ORDER BY count DESC',
        (gallery_id,)
    ).fetchall()
    return render_template('admin/gallery_detail.html',
        gallery=gallery, photos=photos, download_count=download_count,
        share_url=share_url, whatsapp_link=whatsapp_link, invoice=invoice,
        favourites={r['photo_id']: r['count'] for r in favourites},
        is_expired=is_gallery_expired(gallery)
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

            # Upload to Google Drive if enabled
            drive_file_id = None
            if is_drive_enabled():
                mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'tiff': 'image/tiff'}
                mimetype = mime_map.get(ext, 'image/jpeg')
                try:
                    drive_file_id = upload_to_drive(filepath, filename, mimetype)
                    make_file_public(drive_file_id)
                except Exception as e:
                    print(f"Drive upload error: {e}")

            db.execute(
                'INSERT INTO photos (gallery_id, filename, original_name, drive_file_id) VALUES (?, ?, ?, ?)',
                (gallery_id, filename, file.filename, drive_file_id)
            )
            uploaded += 1
    db.commit()
    flash(f'{uploaded} photo(s) uploaded!', 'success')
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
        flash('No customer email set.', 'error')
        return redirect(url_for('admin_gallery_detail', gallery_id=gallery_id))
    share_url = request.host_url.rstrip('/') + '/gallery/' + gallery['token']
    if send_gallery_email(email, gallery['name'], share_url):
        flash(f'Email sent to {email}!', 'success')
    else:
        flash('Failed to send email. Check SMTP settings.', 'error')
    return redirect(url_for('admin_gallery_detail', gallery_id=gallery_id))


@app.route('/admin/gallery/<int:gallery_id>/delete', methods=['POST'])
@login_required
def delete_gallery(gallery_id):
    db = get_db()
    photos = db.execute('SELECT * FROM photos WHERE gallery_id = ?', (gallery_id,)).fetchall()
    for photo in photos:
        for folder in [UPLOAD_FOLDER, THUMBNAIL_FOLDER]:
            path = os.path.join(folder, photo['filename'])
            if os.path.exists(path):
                os.remove(path)
        # Delete from Google Drive
        if is_drive_enabled() and photo['drive_file_id']:
            delete_from_drive(photo['drive_file_id'])
    db.execute('DELETE FROM invoices WHERE gallery_id = ?', (gallery_id,))
    db.execute('DELETE FROM favourites WHERE gallery_id = ?', (gallery_id,))
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
    for folder in [UPLOAD_FOLDER, THUMBNAIL_FOLDER]:
        path = os.path.join(folder, photo['filename'])
        if os.path.exists(path):
            os.remove(path)
    # Delete from Google Drive
    if is_drive_enabled() and photo['drive_file_id']:
        delete_from_drive(photo['drive_file_id'])
    db.execute('DELETE FROM favourites WHERE photo_id = ?', (photo_id,))
    db.execute('DELETE FROM downloads WHERE photo_id = ?', (photo_id,))
    db.execute('DELETE FROM photos WHERE id = ?', (photo_id,))
    db.commit()
    flash('Photo deleted', 'success')
    return redirect(url_for('admin_gallery_detail', gallery_id=photo['gallery_id']))


# ─── Admin Invoice/Payment ──────────────────────────────────────────────────────

@app.route('/admin/gallery/<int:gallery_id>/mark-paid', methods=['POST'])
@login_required
def mark_paid(gallery_id):
    db = get_db()
    db.execute(
        'UPDATE invoices SET status = ?, paid_at = CURRENT_TIMESTAMP WHERE gallery_id = ? AND status = ?',
        ('paid', gallery_id, 'pending')
    )
    db.commit()
    flash('Marked as paid!', 'success')
    return redirect(url_for('admin_gallery_detail', gallery_id=gallery_id))


# ─── Customer Routes ────────────────────────────────────────────────────────────

@app.route('/gallery/<token>/auth', methods=['GET', 'POST'])
def gallery_auth(token):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
    if not gallery:
        abort(404)
    if not gallery['password']:
        return redirect(url_for('customer_gallery', token=token))
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == gallery['password']:
            session[f'gallery_auth_{token}'] = True
            return redirect(url_for('customer_gallery', token=token))
        flash('Incorrect password', 'error')
    return render_template('customer/auth.html', gallery=gallery)


@app.route('/gallery/<token>')
def customer_gallery(token):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
    if not gallery:
        abort(404)

    # Check expiry
    if is_gallery_expired(gallery):
        return render_template('customer/expired.html', gallery=gallery)

    # Check password
    if gallery['password'] and not session.get(f'gallery_auth_{token}'):
        return redirect(url_for('gallery_auth', token=token))

    # Check payment
    if not is_gallery_paid(gallery):
        invoice = db.execute(
            'SELECT * FROM invoices WHERE gallery_id = ? ORDER BY created_at DESC LIMIT 1',
            (gallery['id'],)
        ).fetchone()
        return render_template('customer/payment_required.html', gallery=gallery, invoice=invoice)

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

    # Get favourites for this session
    sid = session.get('_id', session.sid if hasattr(session, 'sid') else 'anon')
    favourite_ids = [row['photo_id'] for row in db.execute(
        'SELECT photo_id FROM favourites WHERE gallery_id = ? AND session_id = ?',
        (gallery['id'], sid)
    ).fetchall()]

    return render_template('customer/gallery.html',
        gallery=gallery, photos=photos,
        downloads_remaining=downloads_remaining,
        downloaded_ids=downloaded_ids,
        favourite_ids=favourite_ids
    )


@app.route('/gallery/<token>/download/<int:photo_id>')
def download_photo(token, photo_id):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
    if not gallery:
        abort(404)
    if is_gallery_expired(gallery):
        abort(403)
    if not is_gallery_paid(gallery):
        abort(403)

    photo = db.execute(
        'SELECT * FROM photos WHERE id = ? AND gallery_id = ?', (photo_id, gallery['id'])
    ).fetchone()
    if not photo:
        abort(404)

    already_downloaded = db.execute(
        'SELECT id FROM downloads WHERE gallery_id = ? AND photo_id = ?',
        (gallery['id'], photo_id)
    ).fetchone()

    if not already_downloaded:
        if gallery['download_limit'] is not None:
            download_count = get_download_count(gallery['id'])
            if download_count >= gallery['download_limit']:
                flash('Download limit reached.', 'error')
                return redirect(url_for('customer_gallery', token=token))
        db.execute(
            'INSERT INTO downloads (gallery_id, photo_id) VALUES (?, ?)',
            (gallery['id'], photo_id)
        )
        db.commit()

    # Serve from Google Drive if available, otherwise local
    if is_drive_enabled() and photo['drive_file_id']:
        buffer = download_from_drive(photo['drive_file_id'])
        return send_file(buffer, mimetype='application/octet-stream',
                         as_attachment=True, download_name=photo['original_name'])

    return send_from_directory(
        app.config['UPLOAD_FOLDER'], photo['filename'],
        download_name=photo['original_name'], as_attachment=True
    )


@app.route('/gallery/<token>/download-zip', methods=['POST'])
def download_zip(token):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
    if not gallery:
        abort(404)
    if is_gallery_expired(gallery):
        abort(403)
    if not is_gallery_paid(gallery):
        abort(403)

    data = request.get_json()
    photo_ids = data.get('photo_ids', []) if data else []
    if not photo_ids:
        return jsonify({'error': 'No photos selected'}), 400

    # Check download limit
    already_downloaded = set(row['photo_id'] for row in db.execute(
        'SELECT DISTINCT photo_id FROM downloads WHERE gallery_id = ?', (gallery['id'],)
    ).fetchall())

    new_downloads = [pid for pid in photo_ids if pid not in already_downloaded]

    if gallery['download_limit'] is not None:
        current_count = len(already_downloaded)
        if current_count + len(new_downloads) > gallery['download_limit']:
            return jsonify({'error': 'Would exceed download limit'}), 403

    # Build zip
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for pid in photo_ids:
            photo = db.execute(
                'SELECT * FROM photos WHERE id = ? AND gallery_id = ?', (pid, gallery['id'])
            ).fetchone()
            if photo:
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo['filename'])
                if os.path.exists(filepath):
                    zf.write(filepath, photo['original_name'])
                    # Record download
                    if pid not in already_downloaded:
                        db.execute(
                            'INSERT INTO downloads (gallery_id, photo_id) VALUES (?, ?)',
                            (gallery['id'], pid)
                        )

    db.commit()
    buffer.seek(0)
    return send_file(buffer, mimetype='application/zip',
                     as_attachment=True, download_name=f"{gallery['name']}.zip")


@app.route('/gallery/<token>/favourite/<int:photo_id>', methods=['POST'])
def toggle_favourite(token, photo_id):
    db = get_db()
    gallery = db.execute('SELECT * FROM galleries WHERE token = ?', (token,)).fetchone()
    if not gallery:
        abort(404)

    sid = session.get('_id', str(uuid.uuid4()))
    if '_id' not in session:
        session['_id'] = sid

    existing = db.execute(
        'SELECT id FROM favourites WHERE gallery_id = ? AND photo_id = ? AND session_id = ?',
        (gallery['id'], photo_id, sid)
    ).fetchone()

    if existing:
        db.execute('DELETE FROM favourites WHERE id = ?', (existing['id'],))
        favourited = False
    else:
        db.execute(
            'INSERT INTO favourites (gallery_id, photo_id, session_id) VALUES (?, ?, ?)',
            (gallery['id'], photo_id, sid)
        )
        favourited = True

    db.commit()
    return jsonify({'favourited': favourited})


# ─── Static file serving ────────────────────────────────────────────────────────

@app.route('/uploads/thumbnails/<filename>')
def serve_thumbnail(filename):
    return send_from_directory(THUMBNAIL_FOLDER, filename)


@app.route('/')
def index():
    return redirect(url_for('admin_login'))


if __name__ == '__main__':
    app.run(debug=True, port=5001)
