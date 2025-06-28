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
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['RESULTS_FOLDER'] = 'results'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Create directories
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RESULTS_FOLDER'], exist_ok=True)

# Global variables for tracking progress
current_task = None

import os

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
    
    def extract_emails(self, text):
        """Extract email addresses from text"""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, text, re.IGNORECASE)
        
        # Filter out common false positives
        filtered_emails = []
        exclude_patterns = [
            '@example.', '@test.', '@placeholder.', '@domain.',
            '@sentry.', '@facebook.', '@twitter.', '@linkedin.',
            'noreply@', 'no-reply@', 'support@', 'info@',
            '@youtube.', '@instagram.', '@tiktok.', '@google.',
            '@microsoft.', '@apple.', '@amazon.', '@adobe.',
            '@github.', '@stackoverflow.', '@reddit.', '@discord.'
        ]
        
        for email in emails:
            email = email.lower().strip()
            if not any(exclude in email for exclude in exclude_patterns):
                if len(email) > 5 and '.' in email.split('@')[1]:
                    filtered_emails.append(email)
        
        return list(set(filtered_emails))
    
    async def create_browser_context(self):
        """Create a browser context with better stealth settings"""
        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(
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
            
            return p, browser, context
            
        except Exception as e:
            logger.error(f"Failed to create browser context: {e}")
            raise
    
    async def search_alternative_engines(self, query, max_results=10):
        """Search using alternative search engines with better error handling"""
        urls = set()
        
        # Try DuckDuckGo with retries
        for attempt in range(self.max_retries):
            try:
                p, browser, context = await self.create_browser_context()
                page = await context.new_page()
                
                # Set page timeout
                page.set_default_timeout(30000)
                
                # DuckDuckGo search
                search_url = f"https://duckduckgo.com/?q={quote(query)}"
                await page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
                
                # Wait for results with fallback
                try:
                    await page.wait_for_selector('a[data-testid="result-title-a"]', timeout=15000)
                except:
                    # Try alternative selector
                    await page.wait_for_selector('h3 a', timeout=10000)
                
                # Extract URLs with multiple selectors
                page_urls = await page.evaluate("""
                    () => {
                        const results = [];
                        const selectors = ['a[data-testid="result-title-a"]', 'h3 a', '.result__a'];
                        
                        for (let selector of selectors) {
                            const links = document.querySelectorAll(selector);
                            for (let link of links) {
                                const href = link.href;
                                if (href && 
                                    !href.includes('duckduckgo.com') && 
                                    !href.includes('youtube.com') &&
                                    !href.includes('facebook.com') &&
                                    !href.includes('twitter.com') &&
                                    href.startsWith('http')) {
                                    results.push(href);
                                }
                            }
                        }
                        return [...new Set(results)];
                    }
                """)
                
                urls.update(page_urls[:max_results])
                await browser.close()
                break  # Success, exit retry loop
                
            except Exception as e:
                logger.error(f"DuckDuckGo search attempt {attempt + 1} failed: {e}")
                if 'browser' in locals():
                    try:
                        await browser.close()
                    except:
                        pass
                
                if attempt == self.max_retries - 1:
                    logger.error(f"DuckDuckGo search failed after {self.max_retries} attempts")
                else:
                    await asyncio.sleep(5)  # Wait before retry
        
        # Try Bing as fallback if DuckDuckGo failed
        if not urls:
            for attempt in range(self.max_retries):
                try:
                    p, browser, context = await self.create_browser_context()
                    page = await context.new_page()
                    
                    search_url = f"https://www.bing.com/search?q={quote(query)}"
                    await page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
                    
                    # Wait for results
                    await page.wait_for_selector('h2 a', timeout=15000)
                    
                    # Extract URLs
                    page_urls = await page.evaluate("""
                        () => {
                            const results = [];
                            const links = document.querySelectorAll('h2 a, .b_title a');
                            for (let link of links) {
                                const href = link.href;
                                if (href && 
                                    !href.includes('bing.com') && 
                                    !href.includes('youtube.com') &&
                                    !href.includes('facebook.com') &&
                                    href.startsWith('http')) {
                                    results.push(href);
                                }
                            }
                            return [...new Set(results)];
                        }
                    """)
                    
                    urls.update(page_urls[:max_results])
                    await browser.close()
                    break
                    
                except Exception as e:
                    logger.error(f"Bing search attempt {attempt + 1} failed: {e}")
                    if 'browser' in locals():
                        try:
                            await browser.close()
                        except:
                            pass
                    
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(5)
        
        return list(urls)[:max_results]
    
    async def scrape_page_for_emails(self, url):
        """Scrape a webpage for email addresses with robust error handling"""
        for attempt in range(self.max_retries):
            try:
                p, browser, context = await self.create_browser_context()
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
                
                # Extract text
                text = soup.get_text(separator=' ', strip=True)
                
                # Also check specific elements that commonly contain emails
                email_selectors = [
                    'a[href^="mailto:"]',
                    '[class*="email"]',
                    '[class*="contact"]',
                    '[id*="email"]',
                    '[id*="contact"]'
                ]
                
                email_elements = []
                for selector in email_selectors:
                    elements = soup.select(selector)
                    email_elements.extend(elements)
                
                element_text = ' '.join([elem.get_text() for elem in email_elements])
                
                # Check href attributes for mailto links
                mailto_links = soup.find_all('a', href=re.compile(r'^mailto:'))
                mailto_emails = [link['href'].replace('mailto:', '') for link in mailto_links]
                
                combined_text = text + ' ' + element_text + ' ' + ' '.join(mailto_emails)
                emails = self.extract_emails(combined_text)
                
                await browser.close()
                return emails
                
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
        """Classify if the name is likely a person or company"""
        company_indicators = [
            'ventures', 'capital', 'fund', 'partners', 'group', 'corp', 'ltd',
            'inc', 'llc', 'bank', 'foundation', 'network', 'holdings', 'management',
            'equity', 'investment', 'angels', 'vc', 'advisory', 'advisors'
        ]
        
        name_lower = name.lower()
        if any(indicator in name_lower for indicator in company_indicators):
            return 'company'
        else:
            return 'person'
    
    async def find_emails_for_investor(self, investor_name):
        """Find emails for a specific investor with multiple strategies"""
        logger.info(f"Searching for: {investor_name}")
        
        investor_type = self.classify_investor_type(investor_name)
        all_emails = set()
        
        # Strategy 1: Search alternative engines
        if investor_type == 'person':
            queries = [
                f'"{investor_name}" email contact',
                f'"{investor_name}" investor contact'
            ]
        else:
            queries = [
                f'"{investor_name}" contact email',
                f'"{investor_name}" team contact'
            ]
        
        for query in queries:
            try:
                logger.info(f"Query: {query}")
                
                # Search using alternative engines
                urls = await self.search_alternative_engines(query, max_results=5)
                logger.info(f"Found {len(urls)} URLs from search engines")
                
                # Scrape each URL
                for url in urls[:3]:  # Limit to first 3 URLs
                    logger.info(f"Scraping: {url}")
                    emails = await self.scrape_page_for_emails(url)
                    if emails:
                        all_emails.update(emails)
                        logger.info(f"Found emails: {emails}")
                    
                    # Delay between page scrapes
                    await asyncio.sleep(random.uniform(3, 6))
                
                # Delay between queries
                await asyncio.sleep(random.uniform(5, 10))
                
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
            
            # Auto-save every 20 investors
            if i % 20 == 0 or i == total:
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

# Flask routes
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
        scraper = InvestorEmailScraper(delay_range=(10, 20))
        
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

# HTML Template
@app.route('/template')
def get_template():
    return '''
<!DOCTYPE html>
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
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Investor Email Scraper</h1>
        
        <div class="file-input">
            <h3>Upload Investor List</h3>
            <input type="file" id="fileInput" accept=".csv,.xlsx" />
            <button onclick="uploadFile()">Upload File</button>
            <p><small>Upload a CSV or Excel file with investor names in the first column</small></p>
        </div>
        
        <div id="fileInfo" style="display: none;">
            <h3>File Information</h3>
            <p>Investors found: <span id="investorCount">0</span></p>
            <div id="investorPreview"></div>
            <button onclick="startScraping()" id="startBtn">Start Scraping</button>
        </div>
        
        <div class="status" id="statusDiv" style="display: none;">
            <h3>Scraping Status</h3>
            <div class="progress-bar">
                <div class="progress" id="progressBar" style="width: 0%"></div>
            </div>
            <p>Progress: <span id="progressText">0/0</span></p>
            <p>Current: <span id="currentInvestor">-</span></p>
            <p>Time elapsed: <span id="timeElapsed">0s</span></p>
            <p>Emails found: <span id="emailsFound">0</span></p>
            <button onclick="stopScraping()" id="stopBtn">Stop Scraping</button>
        </div>
        
        <div id="resultsDiv" style="display: none;">
            <h3>✅ Results Ready</h3>
            <button onclick="downloadResults()" id="downloadBtn">Download Results</button>
        </div>
    </div>

    <script>
        let currentFilename = '';
        let resultsFilename = '';
        let startTime = null;
        let statusInterval = null;
        
        function uploadFile() {
            const fileInput = document.getElementById('fileInput');
            const file = fileInput.files[0];
            
            if (!file) {
                alert('Please select a file');
                return;
            }
            
            const formData = new FormData();
            formData.append('file', file);
            
            fetch('/upload', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    currentFilename = data.filename;
                    document.getElementById('investorCount').textContent = data.count;
                    document.getElementById('investorPreview').innerHTML = 
                        '<p><strong>Preview:</strong> ' + data.preview.join(', ') + 
                        (data.count > 10 ? '...' : '') + '</p>';
                    document.getElementById('fileInfo').style.display = 'block';
                } else {
                    alert('Error: ' + data.error);
                }
            })
            .catch(error => {
                alert('Upload failed: ' + error);
            });
        }
        
        function startScraping() {
            fetch('/start_scraping', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({filename: currentFilename})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('statusDiv').style.display = 'block';
                    document.getElementById('startBtn').disabled = true;
                    startTime = new Date();
                    
                    // Start polling for status updates
                    statusInterval = setInterval(updateStatus, 2000);
                } else {
                    alert('Error: ' + data.error);
                }
            })
            .catch(error => {
                alert('Failed to start scraping: ' + error);
            });
        }
        
        function updateStatus() {
            fetch('/status')
            .then(response => response.json())
            .then(status => {
                const progress = (status.progress / status.total) * 100;
                document.getElementById('progressBar').style.width = progress + '%';
                document.getElementById('progressText').textContent = status.progress + '/' + status.total;
                document.getElementById('currentInvestor').textContent = status.current_investor;
                document.getElementById('emailsFound').textContent = status.emails_found;
                
                // Update time elapsed
                if (startTime) {
                    const elapsed = Math.floor((new Date() - startTime) / 1000);
                    document.getElementById('timeElapsed').textContent = elapsed + 's';
                }
                
                if (!status.running) {
                    clearInterval(statusInterval);
                    resultsFilename = status.results_file;
                    document.getElementById('resultsDiv').style.display = 'block';
                    document.getElementById('statusDiv').classList.add('success');
                }
            })
            .catch(error => {
                console.error('Status update failed:', error);
            });
        }
        
        function stopScraping() {
            fetch('/stop_scraping', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    clearInterval(statusInterval);
                    alert('Scraping stopped');
                }
            });
        }
        
        function downloadResults() {
            if (resultsFilename) {
                window.location.href = '/download/' + resultsFilename;
            }
        }
    </script>
</body>
</html>'''

if __name__ == '__main__':
    # Create templates directory and file
    os.makedirs('templates', exist_ok=True)
    
    # Save the template
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
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Investor Email Scraper</h1>
        
        <div class="file-input">
            <h3>Upload Investor List</h3>
            <input type="file" id="fileInput" accept=".csv,.xlsx" />
            <button onclick="uploadFile()">Upload File</button>
            <p><small>Upload a CSV or Excel file with investor names in the first column</small></p>
        </div>
        
        <div id="fileInfo" style="display: none;">
            <h3>File Information</h3>
            <p>Investors found: <span id="investorCount">0</span></p>
            <div id="investorPreview"></div>
            <button onclick="startScraping()" id="startBtn">Start Scraping</button>
        </div>
        
        <div class="status" id="statusDiv" style="display: none;">
            <h3>Scraping Status</h3>
            <div class="progress-bar">
                <div class="progress" id="progressBar" style="width: 0%"></div>
            </div>
            <p>Progress: <span id="progressText">0/0</span></p>
            <p>Current: <span id="currentInvestor">-</span></p>
            <p>Time elapsed: <span id="timeElapsed">0s</span></p>
            <p>Emails found: <span id="emailsFound">0</span></p>
            <button onclick="stopScraping()" id="stopBtn">Stop Scraping</button>
        </div>
        
        <div id="resultsDiv" style="display: none;">
            <h3>✅ Results Ready</h3>
            <button onclick="downloadResults()" id="downloadBtn">Download Results</button>
        </div>
    </div>

    <script>
        let currentFilename = '';
        let resultsFilename = '';
        let startTime = null;
        let statusInterval = null;
        
        function uploadFile() {
            const fileInput = document.getElementById('fileInput');
            const file = fileInput.files[0];
            
            if (!file) {
                alert('Please select a file');
                return;
            }
            
            const formData = new FormData();
            formData.append('file', file);
            
            fetch('/upload', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    currentFilename = data.filename;
                    document.getElementById('investorCount').textContent = data.count;
                    document.getElementById('investorPreview').innerHTML = 
                        '<p><strong>Preview:</strong> ' + data.preview.join(', ') + 
                        (data.count > 10 ? '...' : '') + '</p>';
                    document.getElementById('fileInfo').style.display = 'block';
                } else {
                    alert('Error: ' + data.error);
                }
            })
            .catch(error => {
                alert('Upload failed: ' + error);
            });
        }
        
        function startScraping() {
            fetch('/start_scraping', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({filename: currentFilename})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('statusDiv').style.display = 'block';
                    document.getElementById('startBtn').disabled = true;
                    startTime = new Date();
                    
                    // Start polling for status updates
                    statusInterval = setInterval(updateStatus, 2000);
                } else {
                    alert('Error: ' + data.error);
                }
            })
            .catch(error => {
                alert('Failed to start scraping: ' + error);
            });
        }
        
        function updateStatus() {
            fetch('/status')
            .then(response => response.json())
            .then(status => {
                const progress = (status.progress / status.total) * 100;
                document.getElementById('progressBar').style.width = progress + '%';
                document.getElementById('progressText').textContent = status.progress + '/' + status.total;
                document.getElementById('currentInvestor').textContent = status.current_investor;
                document.getElementById('emailsFound').textContent = status.emails_found;
                
                // Update time elapsed
                if (startTime) {
                    const elapsed = Math.floor((new Date() - startTime) / 1000);
                    document.getElementById('timeElapsed').textContent = elapsed + 's';
                }
                
                if (!status.running) {
                    clearInterval(statusInterval);
                    resultsFilename = status.results_file;
                    document.getElementById('resultsDiv').style.display = 'block';
                    document.getElementById('statusDiv').classList.add('success');
                }
            })
            .catch(error => {
                console.error('Status update failed:', error);
            });
        }
        
        function stopScraping() {
            fetch('/stop_scraping', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    clearInterval(statusInterval);
                    alert('Scraping stopped');
                }
            });
        }
        
        function downloadResults() {
            if (resultsFilename) {
                window.location.href = '/download/' + resultsFilename;
            }
        }
    </script>
</body>
</html>'''
    
    with open('templates/index.html', 'w') as f:
        f.write(template_content)
    
    # Get port from environment variable (Railway sets this automatically)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)