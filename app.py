from flask import Flask, render_template, request, jsonify, redirect, Response, send_file
import requests
import json
import os
from datetime import datetime
import random
import hashlib
import base64
from premailer import transform
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import uuid
import csv
import io
import re

# Import psycopg2 for PostgreSQL
import psycopg2
from psycopg2 import sql
from urllib.parse import urlparse

# Add dotenv support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print('python-dotenv not installed; .env file will not be loaded automatically.')

app = Flask(__name__)

# --- Configuration for Render Deployment ---
PORT = int(os.environ.get("PORT", 5000))
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    BASE_URL = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}"
else:
    BASE_URL = f"http://localhost:{PORT}"

print(f"Application will use BASE_URL: {BASE_URL}")

# --- Database Configuration (PostgreSQL) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set. Cannot connect to PostgreSQL.")

    result = urlparse(DATABASE_URL)
    conn = psycopg2.connect(
        host=result.hostname,
        port=result.port,
        database=result.path[1:],
        user=result.username,
        password=result.password,
        sslmode='require'
    )
    return conn

# Database initialization (PostgreSQL specific SQL)
def init_db():
    """Initialize PostgreSQL database for A/B testing tracking"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Campaigns table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                company_name TEXT NOT NULL,
                product_name TEXT NOT NULL,
                offer_details TEXT NOT NULL,
                campaign_type TEXT NOT NULL,
                target_audience TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'draft',
                total_recipients INTEGER DEFAULT 0
            )
        ''')

        # Email variations table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_variations (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                variation_name TEXT NOT NULL,
                subject_line TEXT NOT NULL,
                email_body TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')

        # Recipients table with UNIQUE constraint
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recipients (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                email_address TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                variation_assigned TEXT NOT NULL,
                sent_at TIMESTAMP,
                opened_at TIMESTAMP,
                clicked_at TIMESTAMP,
                converted_at TIMESTAMP,
                status TEXT DEFAULT 'pending',
                tracking_id TEXT UNIQUE,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE,
                UNIQUE (campaign_id, email_address)
            )
        ''')

        # A/B test results table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ab_results (
                id SERIAL PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                variation_name TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value REAL NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')

        conn.commit()
        cursor.close()
        print("PostgreSQL database tables checked/created successfully!")
    except Exception as e:
        print(f"Error initializing PostgreSQL database: {e}")
        raise
    finally:
        if conn:
            conn.close()

try:
    init_db()
except Exception as e:
    print(f"FATAL ERROR: Failed to initialize database: {e}")

# Gmail API configuration
SCOPES = ['https://www.googleapis.com/auth/gmail.send']
if os.environ.get('GOOGLE_CREDENTIALS_JSON_B64'):
    try:
        decoded_credentials = base64.b64decode(os.environ['GOOGLE_CREDENTIALS_JSON_B64']).decode('utf-8')
        with open('credentials.json', 'w') as f:
            f.write(decoded_credentials)
        print("credentials.json created from environment variable.")
    except Exception as e:
        print(f"Error decoding GOOGLE_CREDENTIALS_JSON_B64: {e}")

if os.environ.get('GOOGLE_TOKEN_JSON_B64'):
    try:
        decoded_token = base64.b64decode(os.environ['GOOGLE_TOKEN_JSON_B64']).decode('utf-8')
        with open('token.json', 'w') as f:
            f.write(decoded_token)
        print("token.json created from environment variable.")
    except Exception as e:
        print(f"Error decoding GOOGLE_TOKEN_JSON_B64: {e}")


# --- Helper Functions ---

def authenticate_gmail():
    """Authenticate and return Gmail service object"""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                raise Exception("credentials.json not found. Set GOOGLE_CREDENTIALS_JSON_B64 env var.")
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def add_click_tracking(html_body, tracking_id):
    """Add click tracking to links in email body"""
    def replace_link(match):
        original_url = match.group(1)
        # Avoid tracking mailto links or already tracked links
        if original_url.startswith(('mailto:', 'http://', 'https://')) and '/click/' not in original_url:
            tracking_url = f"{BASE_URL}/click/{tracking_id}?url={original_url}"
            return f'href="{tracking_url}"'
        return match.group(0)
    return re.sub(r'href="([^"]*)"', replace_link, html_body)

def create_email_message(to_email, subject, body, tracking_id):
    """Create email message with tracking pixel and click tracking"""
    message = MIMEMultipart('alternative')
    message['to'] = to_email
    message['subject'] = subject

    tracking_pixel = f'<img src="{BASE_URL}/pixel/{tracking_id}" width="1" height="1" alt="" style="display:none;">'
    html_body = body.replace('\n', '<br>')
    html_body = add_click_tracking(html_body, tracking_id)
    html_body += tracking_pixel

    plain_text_body = re.sub('<[^<]+?>', '', body) # Basic HTML to text conversion

    message.attach(MIMEText(plain_text_body, 'plain'))
    message.attach(MIMEText(html_body, 'html'))
    return {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}

def send_email_via_gmail(service, email_message):
    """Send email using Gmail API"""
    try:
        message = service.users().messages().send(userId="me", body=email_message).execute()
        return {'success': True, 'message_id': message['id']}
    except HttpError as error:
        return {'success': False, 'error': str(error)}

def calculate_ab_metrics(campaign_id):
    """Calculate A/B testing metrics for a campaign, showing assigned vs. sent."""
    conn = get_db_connection()
    cursor = conn.cursor()
    metrics = {}

    try:
        # Get all defined variations for the campaign, ensuring they appear even with 0 recipients
        cursor.execute(sql.SQL('''
            SELECT variation_name FROM email_variations
            WHERE campaign_id = %s ORDER BY variation_name
        '''), [campaign_id])
        variations = [row[0] for row in cursor.fetchall()]

        # Get all recipient data for the campaign in one query for efficiency
        cursor.execute(sql.SQL('''
            SELECT variation_assigned, status, opened_at, clicked_at, converted_at
            FROM recipients WHERE campaign_id = %s
        '''), [campaign_id])
        all_recipients_data = cursor.fetchall()

        for variation in variations:
            # Filter the fetched data in Python for this specific variation
            variation_recipients = [r for r in all_recipients_data if r[0] == variation]
            sent_recipients = [r for r in variation_recipients if r[1] == 'sent']

            total_assigned = len(variation_recipients)
            total_sent = len(sent_recipients)
            opened = sum(1 for r in sent_recipients if r[2] is not None)
            clicked = sum(1 for r in sent_recipients if r[3] is not None)
            converted = sum(1 for r in sent_recipients if r[4] is not None)

            metrics[variation] = {
                'total_assigned': total_assigned,
                'total_sent': total_sent,
                'opened': opened,
                'clicked': clicked,
                'converted': converted,
                'open_rate': (opened / total_sent * 100) if total_sent > 0 else 0,
                'click_rate': (clicked / total_sent * 100) if total_sent > 0 else 0,
                'conversion_rate': (converted / total_sent * 100) if total_sent > 0 else 0,
                'click_through_rate': (clicked / opened * 100) if opened > 0 else 0
            }
    finally:
        cursor.close()
        conn.close()
        
    return metrics

def query_groq(prompt):
    """Query the Groq API for content integration."""
    if not GROQ_API_KEY:
        return {'error': 'GROQ_API_KEY not set in environment'}
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama3-70b-8192",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5
    }
    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {str(e)}"}

def generate_email_variations(company_name, product_name, offer_details, campaign_type, target_audience=""):
    """Generate email variations using AI"""
    prompt = f"""You are an expert email marketing copywriter. Create two distinct marketing email variations (A/B test).

**Campaign Details:**
- **Company:** {company_name}
- **Product/Service:** {product_name}
- **Offer:** {offer_details}
- **Campaign Type:** {campaign_type}
- **Audience:** {target_audience or "General customers"}

**Requirements:**
1.  **Subject Line:** Compelling and under 50 characters.
2.  **Email Body:** Persuasive, professional, and clear. Use the placeholder `[FirstName]` for personalization (e.g., `Hi [FirstName],`). If no name is suitable, use a generic greeting like "Hello,".
3.  **Structure:** Each variation must be unique in its psychological approach (e.g., urgency vs. social proof).
4.  **CTA:** Include a clear call-to-action like `[Link: Claim Your Offer]`.
5.  **Format:** Provide the response ONLY in the format below, with no extra commentary.

VARIATION A:
SUBJECT: [Subject for Variation A]
BODY:
[Body for Variation A]

VARIATION B:
SUBJECT: [Subject for Variation B]
BODY:
[Body for Variation B]
"""
    # Using Groq for this generation as well for speed
    result = query_groq(prompt)

    if 'error' in result or not result.get('choices'):
        # Fallback in case of API error
        return {
            'success': False,
            'error': result.get('error', 'Failed to generate variations from AI.')
        }
    
    return {'success': True, 'text': result['choices'][0]['message']['content']}


def parse_email_variations(generated_text):
    """Parse generated text into variation objects"""
    variations = []
    try:
        # Regex to capture Subject and Body for each variation
        pattern = re.compile(r"VARIATION\s+[AB]:\s*SUBJECT:\s*(.*?)\s*BODY:\s*(.*)", re.DOTALL | re.IGNORECASE)
        
        # Split text by the next variation marker to isolate content for each
        parts = re.split(r'VARIATION\s+[B]:', generated_text, flags=re.IGNORECASE)
        
        for i, part in enumerate(parts):
            variation_char = 'A' if i == 0 else 'B'
            full_part = f"VARIATION {variation_char}:{part}" # Re-add prefix for pattern matching
            
            match = pattern.search(full_part)
            if match:
                subject = match.group(1).strip()
                body = match.group(2).strip()
                # Clean up potential split artifacts
                if "VARIATION" in body:
                    body = body.split("VARIATION")[0].strip()

                if subject and body:
                    variations.append({'subject': subject, 'body': body})

    except Exception as e:
        print(f"Error parsing AI response: {e}")

    # Ensure we always have two variations, even if parsing fails
    if len(variations) < 2:
        print("Warning: Could not parse two variations from AI response. Using fallback.")
        return [
            {'subject': 'A Special Offer for You', 'body': 'Hi [FirstName],\n\nCheck out our latest product! [Link: Learn More]'},
            {'subject': 'Don\'t Miss Out!', 'body': 'Hello,\n\nThis exclusive offer is ending soon. [Link: Shop Now]'}
        ]
        
    return variations[:2] # Return exactly two variations

# --- API Routes ---

@app.route('/')
def index():
    """Redirects to the A/B Dashboard"""
    return redirect('/ab-dashboard')

@app.route('/ab-dashboard')
def ab_dashboard():
    """A/B testing dashboard"""
    return render_template('ab_dashboard.html', base_url=BASE_URL)

@app.route('/create-campaign', methods=['POST'])
def create_campaign():
    """Create a new A/B testing campaign"""
    try:
        data = request.get_json()
        required = ['company_name', 'product_name', 'offer_details', 'campaign_type']
        if not all(field in data and data[field] for field in required):
            return jsonify({'success': False, 'error': 'Missing required fields'})

        result = generate_email_variations(
            data['company_name'], data['product_name'], data['offer_details'], 
            data['campaign_type'], data.get('target_audience', '')
        )

        if not result['success']:
            return jsonify(result)

        variations = parse_email_variations(result['text'])
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        campaign_id = str(uuid.uuid4())
        campaign_name = f"{data['company_name']} - {data['product_name']} ({data['campaign_type']})"
        
        cursor.execute(sql.SQL('''
            INSERT INTO campaigns (id, name, company_name, product_name, offer_details, campaign_type, target_audience)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        '''), (
            campaign_id, campaign_name, data['company_name'], data['product_name'],
            data['offer_details'], data['campaign_type'], data.get('target_audience', '')
        ))

        for i, var in enumerate(variations):
            cursor.execute(sql.SQL('''
                INSERT INTO email_variations (id, campaign_id, variation_name, subject_line, email_body)
                VALUES (%s, %s, %s, %s, %s)
            '''), (
                str(uuid.uuid4()), campaign_id, f"Variation_{chr(65+i)}", 
                var['subject'], var['body']
            ))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True, 'campaign_id': campaign_id, 'variations': variations})

    except Exception as e:
        print(f"Error in create_campaign: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload-recipients', methods=['POST'])
def upload_recipients():
    """Upload recipient list for A/B testing with balanced, round-robin assignment."""
    try:
        campaign_id = request.form.get('campaign_id')
        if not campaign_id:
            return jsonify({'success': False, 'error': 'Campaign ID required'})

        if 'file' not in request.files or not request.files['file'].filename:
            return jsonify({'success': False, 'error': 'No file uploaded'})

        file = request.files['file']
        stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
        all_recipients = [row for row in csv.DictReader(stream) if row.get('email', '').strip()]

        if not all_recipients:
            return jsonify({'success': False, 'error': 'No valid recipients with email addresses found in the file.'})

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT variation_name FROM email_variations WHERE campaign_id = %s ORDER BY variation_name ASC'), [campaign_id])
        variations = [row[0] for row in cursor.fetchall()]

        if not variations:
            conn.close()
            return jsonify({'success': False, 'error': 'No variations found for this campaign.'})

        num_variations = len(variations)
        recipients_added = 0
        
        for i, row in enumerate(all_recipients):
            assigned_variation = variations[i % num_variations]
            
            cursor.execute(sql.SQL('''
                INSERT INTO recipients (id, campaign_id, email_address, first_name, last_name, variation_assigned, tracking_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (campaign_id, email_address) DO NOTHING
            '''), (
                str(uuid.uuid4()), campaign_id, row['email'].strip(),
                row.get('first_name', ''), row.get('last_name', ''),
                assigned_variation, str(uuid.uuid4())
            ))
            recipients_added += cursor.rowcount

        # Update campaign total recipients count accurately
        cursor.execute(sql.SQL('SELECT COUNT(id) FROM recipients WHERE campaign_id = %s'), [campaign_id])
        total_recipients = cursor.fetchone()[0]
        cursor.execute(sql.SQL('UPDATE campaigns SET total_recipients = %s WHERE id = %s'), (total_recipients, campaign_id))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'recipients_added': recipients_added,
            'message': f'Successfully uploaded {recipients_added} new recipients.'
        })

    except Exception as e:
        print(f"Error in upload_recipients: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/send-campaign', methods=['POST'])
def send_campaign():
    """Send A/B testing campaign"""
    try:
        campaign_id = request.get_json().get('campaign_id')
        if not campaign_id:
            return jsonify({'success': False, 'error': 'Campaign ID required'})

        gmail_service = authenticate_gmail()
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT variation_name, subject_line, email_body FROM email_variations WHERE campaign_id = %s'), [campaign_id])
        variations = {row[0]: {'subject': row[1], 'body': row[2]} for row in cursor.fetchall()}

        cursor.execute(sql.SQL('''
            SELECT id, email_address, first_name, variation_assigned, tracking_id
            FROM recipients WHERE campaign_id = %s AND status = 'pending'
        '''), [campaign_id])
        recipients_to_send = cursor.fetchall()

        sent_count, errors = 0, []
        print(f"--- Starting to send campaign {campaign_id} to {len(recipients_to_send)} recipients ---")

        for rid, email, fname, variation, tid in recipients_to_send:
            try:
                content = variations[variation]
                body = content['body']
                # Personalize email body
                if fname:
                    body = body.replace('[FirstName]', fname)
                else:
                    # Fallback for missing name
                    body = body.replace('Hi [FirstName],', 'Hi,')
                    body = body.replace('[FirstName]', 'there')
                
                email_message = create_email_message(email, content['subject'], body, tid)
                result = send_email_via_gmail(gmail_service, email_message)

                if result['success']:
                    cursor.execute(sql.SQL("UPDATE recipients SET status = 'sent', sent_at = CURRENT_TIMESTAMP WHERE id = %s"), [rid])
                    sent_count += 1
                else:
                    errors.append(f'{email}: {result["error"]}')
                    cursor.execute(sql.SQL("UPDATE recipients SET status = 'failed' WHERE id = %s"), [rid])
            
            except Exception as e:
                errors.append(f'{email}: {str(e)}')
                cursor.execute(sql.SQL("UPDATE recipients SET status = 'failed' WHERE id = %s"), [rid])
        
        cursor.execute(sql.SQL("UPDATE campaigns SET status = 'sent' WHERE id = %s"), [campaign_id])
        conn.commit()
        
        print(f"--- Campaign sending finished. Sent: {sent_count}, Failed: {len(errors)} ---")
        return jsonify({'success': True, 'sent_count': sent_count, 'errors': errors[:10]})

    except Exception as e:
        print(f"Error in send_campaign: {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if 'conn' in locals() and conn:
            cursor.close()
            conn.close()

# --- Tracking and Data Routes ---
@app.route('/campaigns')
def list_campaigns():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(sql.SQL('SELECT id, name, status, total_recipients, created_at FROM campaigns ORDER BY created_at DESC'))
    campaigns = [{'id': r[0], 'name': r[1], 'status': r[2], 'total_recipients': r[3], 'created_at': r[4].isoformat()} for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    return jsonify({'success': True, 'campaigns': campaigns})

@app.route('/campaign-results/<campaign_id>')
def campaign_results(campaign_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql.SQL('SELECT name, status, total_recipients FROM campaigns WHERE id = %s'), [campaign_id])
        campaign_data = cursor.fetchone()
        cursor.close()
        conn.close()

        if not campaign_data:
            return jsonify({'success': False, 'error': 'Campaign not found'})
        
        metrics = calculate_ab_metrics(campaign_id)
        
        return jsonify({
            'success': True,
            'campaign': {'name': campaign_data[0], 'status': campaign_data[1], 'total_recipients': campaign_data[2]},
            'metrics': metrics
        })
    except Exception as e:
        print(f"Error in campaign_results: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/pixel/<tracking_id>')
def tracking_pixel(tracking_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql.SQL('UPDATE recipients SET opened_at = CURRENT_TIMESTAMP WHERE tracking_id = %s AND opened_at IS NULL'), [tracking_id])
        conn.commit()
    except Exception as e:
        print(f"Error tracking pixel {tracking_id}: {e}")
    finally:
        if conn:
            cursor.close()
            conn.close()
    
    pixel = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    return Response(pixel, mimetype='image/gif')

@app.route('/click/<tracking_id>')
def track_click(tracking_id):
    original_url = request.args.get('url', BASE_URL)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql.SQL('UPDATE recipients SET clicked_at = CURRENT_TIMESTAMP WHERE tracking_id = %s AND clicked_at IS NULL'), [tracking_id])
        conn.commit()
    except Exception as e:
        print(f"Error tracking click {tracking_id}: {e}")
    finally:
        if conn:
            cursor.close()
            conn.close()
            
    return redirect(original_url)

# --- Finalize Mail Endpoints --- (Note: This section seems to be a separate experimental feature)

@app.route('/integrate-content-template', methods=['POST'])
def integrate_content_template():
    # This function uses Groq to merge text content into an HTML template
    data = request.get_json()
    content = data.get('content')
    template_html = data.get('template_html')

    if not content or not template_html:
        return jsonify({'success': False, 'error': 'Missing content or template_html'})

    prompt = f"""Integrate the following plain text content into the provided HTML template.
- Replace the placeholder content in the HTML with the new content.
- Preserve all existing HTML structure, styles, and classes.
- Ensure the final output is only the raw, complete HTML code.

**CONTENT:**
{content}

**TEMPLATE_HTML:**
{template_html}
"""
    result = query_groq(prompt)
    if 'error' in result or not result.get('choices'):
        return jsonify({'success': False, 'error': result.get('error', 'AI integration failed')})

    raw_html = result['choices'][0]['message']['content']
    
    # Clean up potential markdown code blocks from the AI response
    if "```html" in raw_html:
        raw_html = raw_html.split("```html", 1)[-1]
    if "```" in raw_html:
        raw_html = raw_html.split("```", 1)[0]
    
    # Inline CSS for maximum email client compatibility
    finalized_html = transform(raw_html.strip())
    return jsonify({'success': True, 'finalized_html': finalized_html})

if __name__ == '__main__':
    print("🚀 Starting A/B Testing Email Marketing App")
    app.run(debug=True, host='0.0.0.0', port=PORT)
