import asyncio
import aiohttp
import re
import csv
import time
import random
import os
import pandas as pd
from urllib.parse import quote, urljoin
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import logging
import json
from typing import List, Dict, Set
from flask import Flask, render_template, request, send_file, jsonify, redirect, url_for
from werkzeug.utils import secure_filename
import threading
from datetime import datetime
import traceback

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'data/uploads'
app.config['RESULTS_FOLDER'] = 'data/results'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Create directories
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RESULTS_FOLDER'], exist_ok=True)

# Global variables for tracking progress
current_task = None

@app.route('/health')
def health_check():
    return jsonify({'status': 'ok', 'message': 'Application is running'})

task_status = {
    'running': False,
    'current_investor': '',
    'progress': 0,
    'total': 0,
    'results_file': '',
    'errors': [],
    'start_time': None,
    'emails_found': 0
}

class InvestorEmailScraper:
    def __init__(self, delay_range=(8, 15), max_retries=3):
        self.delay_range = delay_range
        self.max_retries = max_retries
        self.session = None
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0'
        ]
        self.playwright_instance = None
        
    def parse_investor_list(self, raw_text):
        """Parse the raw investor list into individual names/companies"""
        investors = []
        
        # Replace various separators with newlines
        text = raw_text.replace('•', '\n')
        text = re.sub(r'(?<=[a-z])(?=[A-Z][A-Z])', '\n', text)
        text = re.sub(r'([a-z])([A-Z][a-z])', r'\1\n\2', text)
        
        # Split into lines and clean
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            if line and len(line) > 2:
                investors.append(line)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_investors = []
        for investor in investors:
            if investor.lower() not in seen:
                seen.add(investor.lower())
                unique_investors.append(investor)
        
        return unique_investors
    
    def extract_emails(self, text, investor_name=None):
        """Extract and validate email addresses from text"""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, text, re.IGNORECASE)
        
        # Enhanced filtering for better quality emails
        filtered_emails = []
        exclude_patterns = [
            # Generic/system emails
            '@example.', '@test.', '@placeholder.', '@domain.', '@temp.',
            'noreply@', 'no-reply@', 'donotreply@', 'support@', 'info@',
            'hello@', 'contact@', 'admin@', 'webmaster@', 'sales@',
            'marketing@', 'help@', 'service@', 'customerservice@',
            
            # Social media and big tech
            '@sentry.', '@facebook.', '@twitter.', '@linkedin.',
            '@youtube.', '@instagram.', '@tiktok.', '@google.',
            '@microsoft.', '@apple.', '@amazon.', '@adobe.',
            '@github.', '@stackoverflow.', '@reddit.', '@discord.',
            '@slack.', '@zoom.', '@teams.', '@skype.',
            
            # Tracking and analytics
            '@traxcn.', '@analytics.', '@tracking.', '@pixel.',
            '@googletagmanager.', '@googleanalytics.', '@hotjar.',
            '@mixpanel.', '@segment.', '@amplitude.', '@intercom.',
            
            # Common false positives
            'hi@traxcn', 'track@', 'pixel@', 'img@', 'image@',
            'static@', 'cdn@', 'assets@', 'mail@mailgun',
            'bounce@', '@mailgun.', '@sendgrid.', '@mailchimp.',
            
            # Newsletter/marketing platforms
            '@constantcontact.', '@aweber.', '@convertkit.',
            '@activecampaign.', '@klaviyo.', '@mailerlite.',
        ]
        
        # Domain quality filters
        low_quality_domains = [
            'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
            'aol.com', 'live.com', 'msn.com', 'icloud.com'
        ]
        
        for email in emails:
            email = email.lower().strip()
            
            # Skip if matches exclude patterns
            if any(exclude in email for exclude in exclude_patterns):
                continue
                
            # Basic validation
            if len(email) < 5 or '@' not in email:
                continue
                
            domain_part = email.split('@')[1]
            
            # Skip if domain doesn't have proper extension
            if '.' not in domain_part or len(domain_part.split('.')[-1]) < 2:
                continue
            
            # Skip obvious generic/low quality emails for companies
            if investor_name and self.classify_investor_type(investor_name) == 'company':
                if email.split('@')[1] in low_quality_domains:
                    continue
            
            # Skip emails with too many dots or suspicious patterns
            if email.count('.') > 3 or email.count('-') > 2:
                continue
                
            # Skip emails that are just numbers
            local_part = email.split('@')[0]
            if local_part.isdigit():
                continue
                
            filtered_emails.append(email)
        
        return list(set(filtered_emails))

    async def close(self):
        """Closes the Playwright instance."""
        if self.playwright_instance:
            await self.playwright_instance.stop()
            self.playwright_instance = None
            logger.info("Playwright instance closed.")
    
    async def create_browser_context(self):
        """Create a browser context with better stealth settings"""
        try:
            if self.playwright_instance is None:
                self.playwright_instance = await async_playwright().start()
            
            browser = await self.playwright_instance.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-web-security',
                    '--disable-features=VizDisplayCompositor',
                    '--disable-dev-shm-usage',
                    '--no-first-run',
                    '--disable-default-apps',
                    '--disable-background-networking',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--disable-gpu',
                    '--disable-software-rasterizer'
                ]
            )
            
            context = await browser.new_context(
                user_agent=random.choice(self.user_agents),
                viewport={'width': 1920, 'height': 1080},
                locale='en-US',
                timezone_id='America/New_York',
                ignore_https_errors=True
            )
            
            return browser, context
            
        except Exception as e:
            logger.error(f"Failed to create browser context: {e}")
            raise
    
    async def search_alternative_engines(self, query, max_results=15):
        """Enhanced search with multiple engines and better URL filtering"""
        urls = set()
        
        # Multiple search engines for better coverage
        search_engines = [
            {
                'name': 'DuckDuckGo',
                'url': f"https://duckduckgo.com/?q={quote(query)}",
                'selectors': ['a[data-testid="result-title-a"]', 'h3 a', '.result__a']
            },
            {
                'name': 'Bing',
                'url': f"https://www.bing.com/search?q={quote(query)}",
                'selectors': ['h2 a', '.b_algo h2 a', '.b_title a']
            }
        ]
        
        for engine in search_engines:
            for attempt in range(self.max_retries):
                browser = context = page = None
                try:
                    browser, context = await self.create_browser_context()
                    page = await context.new_page()
                    
                    # Set page timeout
                    page.set_default_timeout(30000)
                    
                    logger.info(f"Searching {engine['name']} for: {query}")
                    await page.goto(engine['url'], wait_until='domcontentloaded', timeout=30000)
                    
                    # Wait for results
                    await asyncio.sleep(3)
                    
                    # Extract URLs with quality filtering
                    page_urls = await page.evaluate(f"""
                        () => {{
                            const results = [];
                            const selectors = {json.dumps(engine['selectors'])};
                            
                            for (let selector of selectors) {{
                                const links = document.querySelectorAll(selector);
                                for (let link of links) {{
                                    const href = link.href;
                                    if (href && href.startsWith('http')) {{
                                        // Filter out low-quality domains
                                        const url = new URL(href);
                                        const domain = url.hostname.toLowerCase();
                                        
                                        // Skip social media and low-quality sites
                                        if (!domain.includes('youtube.com') &&
                                            !domain.includes('facebook.com') &&
                                            !domain.includes('twitter.com') &&
                                            !domain.includes('instagram.com') &&
                                            !domain.includes('tiktok.com') &&
                                            !domain.includes('pinterest.com') &&
                                            !domain.includes('reddit.com') &&
                                            !domain.includes('wikipedia.org') &&
                                            !domain.includes('duckduckgo.com') &&
                                            !domain.includes('bing.com')) {{
                                            results.push(href);
                                        }}
                                    }}
                                }}
                            }}
                            return [...new Set(results)];
                        }}
                    """)
                    
                    # Prioritize company websites and professional platforms
                    prioritized_urls = []
                    other_urls = []
                    
                    for url in page_urls:
                        domain = url.split('/')[2].lower()
                        if any(indicator in domain for indicator in [
                            'capital', 'ventures', 'partners', 'fund', 'invest',
                            'linkedin.com', 'crunchbase.com', 'pitchbook.com'
                        ]):
                            prioritized_urls.append(url)
                        else:
                            other_urls.append(url)
                    
                    # Add prioritized URLs first
                    urls.update(prioritized_urls[:5])
                    urls.update(other_urls[:max_results-len(prioritized_urls)])
                    
                    break # Break on success

                except Exception as e:
                    logger.error(f"{engine['name']} search attempt {attempt + 1} failed: {e}")
                    if attempt == self.max_retries - 1:
                        logger.error(f"Max retries reached for {engine['name']} query: {query}")
                finally:
                    if page:
                        await page.close()
                    if context:
                        await context.close()
                    if browser:
                        await browser.close()
            
            # Delay between search engines
            if len(urls) < max_results:
                await asyncio.sleep(random.uniform(3, 5))

        return list(urls)
                
    async def scrape_page_for_emails(self, url, investor_name=None):
        """Enhanced page scraping with better email extraction"""
        for attempt in range(self.max_retries):
            try:
                browser, context = await self.create_browser_context()
                page = await context.new_page()
                
                # Set page timeout
                page.set_default_timeout(30000)
                
                # Navigate to page
                response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                
                # Check if page loaded successfully
                if response.status >= 400:
                    logger.warning(f"HTTP {response.status} for {url}")
                    await browser.close()
                    continue
                
                # Wait for dynamic content
                await page.wait_for_timeout(3000)
                
                # Get page content
                content = await page.content()
                
                # Parse with BeautifulSoup
                soup = BeautifulSoup(content, 'html.parser')
                
                # Remove unwanted elements
                for element in soup(['script', 'style', 'nav', 'footer', 'header', 'advertisement']):
                    element.decompose()
                
                # Extract text from high-value sections first
                priority_selectors = [
                    '[class*="team"]', '[class*="contact"]', '[class*="about"]',
                    '[class*="leadership"]', '[class*="management"]', '[class*="founder"]',
                    '[class*="partner"]', '[id*="team"]', '[id*="contact"]',
                    '[id*="about"]', 'main', 'article'
                ]
                
                priority_text = ""
                for selector in priority_selectors:
                    elements = soup.select(selector)
                    for element in elements:
                        priority_text += element.get_text(separator=' ', strip=True) + " "
                
                # Get all text as fallback
                all_text = soup.get_text(separator=' ', strip=True)
                
                # Extract emails from mailto links first (highest quality)
                mailto_emails = []
                mailto_links = soup.find_all('a', href=re.compile(r'^mailto:'))
                for link in mailto_links:
                    email = link['href'].replace('mailto:', '').split('?')[0]  # Remove query params
                    mailto_emails.append(email)
                
                # Extract emails from text
                priority_emails = self.extract_emails(priority_text, investor_name)
                all_emails = self.extract_emails(all_text, investor_name)
                
                # Combine and prioritize
                combined_emails = list(set(mailto_emails + priority_emails + all_emails))
                
                await browser.close()
                return combined_emails
                
            except Exception as e:
                logger.error(f"Error scraping {url} (attempt {attempt + 1}): {e}")
                if 'browser' in locals():
                    try:
                        await browser.close()
                    except:
                        pass
                
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(3)
                    
        return []
    
    def classify_investor_type(self, name):
        """Enhanced investor type classification"""
        company_indicators = [
            'ventures', 'capital', 'fund', 'partners', 'group', 'corp', 'ltd',
            'inc', 'llc', 'bank', 'foundation', 'network', 'holdings', 'management',
            'equity', 'investment', 'angels', 'vc', 'advisory', 'advisors',
            'family office', 'wealth', 'asset', 'private equity', 'venture capital'
        ]
        
        name_lower = name.lower()
        if any(indicator in name_lower for indicator in company_indicators):
            return 'company'
        else:
            return 'person'
    
    async def find_emails_for_investor(self, investor_name):
        """Enhanced email finding with better search strategies"""
        logger.info(f"Searching for: {investor_name}")
        
        investor_type = self.classify_investor_type(investor_name)
        all_emails = set()
        
        # Enhanced search queries
        if investor_type == 'person':
            queries = [
                f'"{investor_name}" email contact investor',
                f'"{investor_name}" contact information',
                f'"{investor_name}" linkedin investor email',
                f'"{investor_name}" venture capital email'
            ]
        else:
            queries = [
                f'"{investor_name}" contact email team',
                f'"{investor_name}" investment contact',
                f'"{investor_name}" portfolio team email',
                f'"{investor_name}" partners contact'
            ]
        
        for query in queries:
            try:
                logger.info(f"Query: {query}")
                
                # Search using alternative engines
                urls = await self.search_alternative_engines(query, max_results=8)
                logger.info(f"Found {len(urls)} URLs from search engines")
                
                # Scrape each URL with prioritization
                for i, url in enumerate(urls[:5]):  # Limit to first 5 URLs
                    logger.info(f"Scraping ({i+1}/5): {url}")
                    emails = await self.scrape_page_for_emails(url, investor_name)
                    if emails:
                        all_emails.update(emails)
                        logger.info(f"Found emails: {emails}")
                        
                        # If we found good emails from official sources, we can be less aggressive
                        if len(emails) >= 2 and any('linkedin.com' in url or 
                                                   investor_name.lower().replace(' ', '') in url.lower() 
                                                   for url in [url]):
                            break
                    
                    # Delay between page scrapes
                    await asyncio.sleep(random.uniform(3, 6))
                
                # If we found emails, reduce delay for next query
                if all_emails:
                    await asyncio.sleep(random.uniform(2, 4))
                else:
                    await asyncio.sleep(random.uniform(5, 8))
                
            except Exception as e:
                logger.error(f"Error processing query '{query}': {e}")
        
        return list(all_emails)
    
    async def process_all_investors(self, investor_list, output_file, progress_callback=None):
        """Process the entire list of investors with better error handling"""
        results = []
        total = len(investor_list)
        
        logger.info(f"Processing {total} investors...")
        
        for i, investor in enumerate(investor_list, 1):
            logger.info(f"\n[{i}/{total}] Processing: {investor}")
            
            # Update progress callback
            if progress_callback:
                progress_callback(i, total, investor)
            
            try:
                emails = await self.find_emails_for_investor(investor)
                
                result = {
                    'investor_name': investor,
                    'type': self.classify_investor_type(investor),
                    'emails_found': len(emails),
                    'emails': '; '.join(emails) if emails else 'None found',
                    'status': 'Success',
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                
                logger.info(f"✓ Found {len(emails)} emails for {investor}")
                
            except Exception as e:
                logger.error(f"✗ Error processing {investor}: {e}")
                result = {
                    'investor_name': investor,
                    'type': self.classify_investor_type(investor),
                    'emails_found': 0,
                    'emails': f'Error: {str(e)}',
                    'status': 'Error',
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
            
            results.append(result)
            
            # Auto-save every 10 investors (more frequent saves)
            if i % 10 == 0 or i == total:
                self.save_results(results, output_file)
                logger.info(f"Progress auto-saved: {i}/{total} completed")
            
            # Delay between investors
            if i < total:  # Don't delay after the last investor
                delay = random.uniform(*self.delay_range)
                logger.info(f"Waiting {delay:.1f} seconds before next investor...")
                await asyncio.sleep(delay)
        
        return results
    
    def save_results(self, results, filename):
        """Save results to CSV file"""
        filepath = os.path.join(app.config['RESULTS_FOLDER'], filename)
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            fieldnames = ['investor_name', 'type', 'emails_found', 'emails', 'status', 'timestamp']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        logger.info(f"Results saved to {filepath}")

# Flask routes (keeping the same as before)
@app.route('/')
def index():
    return render_template('index.html', status=task_status)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if file and (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Parse the file
        try:
            if filename.endswith('.csv'):
                df = pd.read_csv(filepath)
            else:
                df = pd.read_excel(filepath)
            
            # Get first column as investor names
            investor_names = df.iloc[:, 0].dropna().astype(str).tolist()
            
            return jsonify({
                'success': True,
                'filename': filename,
                'count': len(investor_names),
                'preview': investor_names[:10]
            })
            
        except Exception as e:
            return jsonify({'error': f'Error parsing file: {str(e)}'}), 400
    
    return jsonify({'error': 'Invalid file format. Please upload CSV or Excel file.'}), 400

@app.route('/start_scraping', methods=['POST'])
def start_scraping():
    global current_task, task_status
    
    if task_status['running']:
        return jsonify({'error': 'A scraping task is already running'}), 400
    
    data = request.json
    filename = data.get('filename')
    
    if not filename:
        return jsonify({'error': 'No filename provided'}), 400
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    
    # Parse the file
    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(filepath)
        else:
            df = pd.read_excel(filepath)
        
        investor_names = df.iloc[:, 0].dropna().astype(str).tolist()
        
        # Reset task status
        task_status.update({
            'running': True,
            'current_investor': '',
            'progress': 0,
            'total': len(investor_names),
            'results_file': f'results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv',
            'errors': [],
            'start_time': datetime.now(),
            'emails_found': 0
        })
        
        # Start scraping in background
        current_task = threading.Thread(target=run_scraping_task, args=(investor_names,))
        current_task.start()
        
        return jsonify({'success': True, 'message': 'Scraping started'})
        
    except Exception as e:
        return jsonify({'error': f'Error starting scraping: {str(e)}'}), 500

@app.route('/status')
def get_status():
    return jsonify(task_status)

@app.route('/download/<filename>')
def download_file(filename):
    filepath = os.path.join(app.config['RESULTS_FOLDER'], filename)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True)
    return jsonify({'error': 'File not found'}), 404

@app.route('/stop_scraping', methods=['POST'])
def stop_scraping():
    global task_status
    task_status['running'] = False
    return jsonify({'success': True, 'message': 'Scraping stopped'})

def run_scraping_task(investor_names):
    """Run the scraping task in a separate thread"""
    def progress_callback(current, total, investor_name):
        task_status.update({
            'current_investor': investor_name,
            'progress': current
        })
    
    try:
        scraper = InvestorEmailScraper(delay_range=(12, 25))  # Increased delays for better stealth
        
        # Run async task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        results = loop.run_until_complete(
            scraper.process_all_investors(
                investor_names, 
                task_status['results_file'],
                progress_callback
            )
        )
        
        # Update final status
        emails_found = sum(1 for r in results if r['emails_found'] > 0)
        task_status.update({
            'running': False,
            'current_investor': 'Completed',
            'emails_found': emails_found
        })
        
    except Exception as e:
        logger.error(f"Scraping task failed: {e}")
        task_status.update({
            'running': False,
            'current_investor': f'Error: {str(e)}'
        })
    finally:
        # Ensure scraper and loop are closed regardless of success or failure
        if 'scraper' in locals() and scraper:
            try:
                loop.run_until_complete(scraper.close())
            except Exception as close_e:
                logger.error(f"Error closing scraper: {close_e}")
        if 'loop' in locals() and loop:
            loop.close()

# HTML Template (keeping the same as before)
if __name__ == '__main__':
    # Create templates directory and file
    os.makedirs('templates', exist_ok=True)
    
    # Save the template (same as your original)
    template_content = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Investor Email Scraper</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .progress-bar {
            width: 100%;
            height: 20px;
            background-color: #e0e0e0;
            border-radius: 10px;
            overflow: hidden;
            margin: 10px 0;
        }
        .progress {
            height: 100%;
            background-color: #4CAF50;
            transition: width 0.3s ease;
        }
        .status {
            margin: 20px 0;
            padding: 15px;
            border-radius: 5px;
            background-color: #f0f8ff;
            border-left: 4px solid #2196F3;
        }
        .error {
            background-color: #ffe6e6;
            border-left-color: #f44336;
        }
        .success {
            background-color: #e8f5e8;
            border-left-color: #4CAF50;
        }
        button {
            background-color: #2196F3;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            margin: 5px;
        }
        button:hover {
            background-color: #1976D2;
        }
        button:disabled {
            background-color: #cccccc;
            cursor: not-allowed;
        }
        .file-input {
            margin: 20px 0;
            padding: 10px;
            border: 2px dashed #ccc;
            border-radius: 5px;
            text-align: center;
        }
        input[type="file"] {
            margin: 10px 0;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }
        .stat-card {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-number {
            font-size: 2em;
            font-weight: bold;
            color: #2196F3;
        }
        .log-container {
            max-height: 300px;
            overflow-y: auto;
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin: 20px 0;
            font-family: monospace;
            font-size: 12px;
        }
        .preview {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin: 10px 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Investor Email Scraper</h1>
        
        <div class="file-input">
            <h3>Upload Investor List</h3>
            <input type="file" id="fileInput" accept=".csv,.xlsx" />
            <button onclick="uploadFile()">Upload File</button>
            <p><small>Supported formats: CSV, Excel (.xlsx)</small></p>
        </div>

        <div id="filePreview" style="display: none;">
            <h3>File Preview</h3>
            <div class="preview">
                <p><strong>File:</strong> <span id="fileName"></span></p>
                <p><strong>Total Investors:</strong> <span id="totalCount"></span></p>
                <div id="previewList"></div>
            </div>
            <button onclick="startScraping()" id="startBtn">Start Scraping</button>
        </div>

        <div id="scrapingStatus" style="display: none;">
            <h3>Scraping Progress</h3>
            <div class="stats">
                <div class="stat-card">
                    <div class="stat-number" id="progressCount">0</div>
                    <div>Processed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number" id="totalProgress">0</div>
                    <div>Total</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number" id="emailsFound">0</div>
                    <div>Emails Found</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number" id="successRate">0%</div>
                    <div>Success Rate</div>
                </div>
            </div>
            
            <div class="progress-bar">
                <div class="progress" id="progressBar" style="width: 0%"></div>
            </div>
            
            <div class="status" id="currentStatus">
                <strong>Current:</strong> <span id="currentInvestor">Ready to start...</span>
            </div>

            <div class="log-container" id="logContainer">
                <div>Scraping logs will appear here...</div>
            </div>

            <button onclick="stopScraping()" id="stopBtn" style="background-color: #f44336;">Stop Scraping</button>
            <button onclick="downloadResults()" id="downloadBtn" style="display: none;">Download Results</button>
        </div>

        <div id="completedStatus" style="display: none;">
            <div class="status success">
                <h3>✅ Scraping Completed!</h3>
                <p>Successfully processed all investors. You can download the results below.</p>
                <button onclick="downloadResults()" class="download-btn">Download Results CSV</button>
                <button onclick="resetApp()" style="background-color: #666;">Start New Scraping</button>
            </div>
        </div>
    </div>

    <script>
        let uploadedFile = null;
        let scrapingInterval = null;
        let logs = [];

        function addLog(message) {
            const timestamp = new Date().toLocaleTimeString();
            logs.push(`[${timestamp}] ${message}`);
            if (logs.length > 100) logs.shift(); // Keep only last 100 logs
            
            const logContainer = document.getElementById('logContainer');
            logContainer.innerHTML = logs.join('<br>');
            logContainer.scrollTop = logContainer.scrollHeight;
        }

        async function uploadFile() {
            const fileInput = document.getElementById('fileInput');
            const file = fileInput.files[0];
            
            if (!file) {
                alert('Please select a file');
                return;
            }

            const formData = new FormData();
            formData.append('file', file);

            try {
                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });

                const result = await response.json();

                if (result.success) {
                    uploadedFile = result.filename;
                    document.getElementById('fileName').textContent = file.name;
                    document.getElementById('totalCount').textContent = result.count;
                    
                    const previewList = document.getElementById('previewList');
                    previewList.innerHTML = '<strong>Preview (first 10):</strong><br>' + 
                        result.preview.map(name => `• ${name}`).join('<br>');
                    
                    document.getElementById('filePreview').style.display = 'block';
                    addLog(`File uploaded successfully: ${result.count} investors found`);
                } else {
                    alert('Upload failed: ' + result.error);
                }
            } catch (error) {
                alert('Upload error: ' + error.message);
            }
        }

        async function startScraping() {
            if (!uploadedFile) {
                alert('Please upload a file first');
                return;
            }

            try {
                const response = await fetch('/start_scraping', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ filename: uploadedFile })
                });

                const result = await response.json();

                if (result.success) {
                    document.getElementById('filePreview').style.display = 'none';
                    document.getElementById('scrapingStatus').style.display = 'block';
                    document.getElementById('startBtn').disabled = true;
                    
                    addLog('Scraping started...');
                    startStatusUpdates();
                } else {
                    alert('Failed to start scraping: ' + result.error);
                }
            } catch (error) {
                alert('Error: ' + error.message);
            }
        }

        async function stopScraping() {
            try {
                const response = await fetch('/stop_scraping', {
                    method: 'POST'
                });

                const result = await response.json();
                if (result.success) {
                    addLog('Scraping stopped by user');
                    stopStatusUpdates();
                }
            } catch (error) {
                console.error('Error stopping scraping:', error);
            }
        }

        function startStatusUpdates() {
            scrapingInterval = setInterval(updateStatus, 2000);
        }

        function stopStatusUpdates() {
            if (scrapingInterval) {
                clearInterval(scrapingInterval);
                scrapingInterval = null;
            }
        }

        async function updateStatus() {
            try {
                const response = await fetch('/status');
                const status = await response.json();

                document.getElementById('progressCount').textContent = status.progress;
                document.getElementById('totalProgress').textContent = status.total;
                document.getElementById('emailsFound').textContent = status.emails_found;
                
                const successRate = status.progress > 0 ? 
                    Math.round((status.emails_found / status.progress) * 100) : 0;
                document.getElementById('successRate').textContent = successRate + '%';

                const progressPercent = status.total > 0 ? 
                    (status.progress / status.total) * 100 : 0;
                document.getElementById('progressBar').style.width = progressPercent + '%';

                document.getElementById('currentInvestor').textContent = 
                    status.current_investor || 'Processing...';

                // Add log for current investor
                if (status.current_investor && status.current_investor !== 'Completed') {
                    addLog(`Processing: ${status.current_investor}`);
                }

                if (!status.running) {
                    stopStatusUpdates();
                    
                    if (status.current_investor === 'Completed') {
                        document.getElementById('scrapingStatus').style.display = 'none';
                        document.getElementById('completedStatus').style.display = 'block';
                        addLog('✅ Scraping completed successfully!');
                    } else {
                        addLog('❌ Scraping stopped or encountered an error');
                        document.getElementById('downloadBtn').style.display = 'inline-block';
                    }
                }

            } catch (error) {
                console.error('Error updating status:', error);
                addLog('Error updating status: ' + error.message);
            }
        }

        function downloadResults() {
            // Get the results filename from the current status
            fetch('/status')
                .then(response => response.json())
                .then(status => {
                    if (status.results_file) {
                        window.location.href = `/download/${status.results_file}`;
                        addLog(`Downloading results: ${status.results_file}`);
                    } else {
                        alert('No results file available');
                    }
                })
                .catch(error => {
                    alert('Error downloading results: ' + error.message);
                });
        }

        function resetApp() {
            uploadedFile = null;
            logs = [];
            document.getElementById('filePreview').style.display = 'none';
            document.getElementById('scrapingStatus').style.display = 'none';
            document.getElementById('completedStatus').style.display = 'none';
            document.getElementById('fileInput').value = '';
            document.getElementById('startBtn').disabled = false;
            addLog('Application reset - ready for new upload');
        }

        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            addLog('Investor Email Scraper initialized');
        });
    </script>
</body>
</html>
'''
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)