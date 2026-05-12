from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
import os
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import re
import tempfile
import sqlite3
from datetime import datetime
import json

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max
app.config['UPLOAD_FOLDER'] = tempfile.gettempdir()
app.config['DATABASE'] = 'epubs.db'

ALLOWED_EXTENSIONS = {'epub'}
MIN_CHAPTER_WORDS = 300

def get_db():
    """Get database connection."""
    db = sqlite3.connect(app.config['DATABASE'])
    db.row_factory = sqlite3.Row
    return db

def init_db():
    """Initialize database with schema."""
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS epubs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            title TEXT,
            total_chapters INTEGER,
            total_words INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epub_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            title TEXT NOT NULL,
            word_count INTEGER NOT NULL,
            preview TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (epub_id) REFERENCES epubs(id) ON DELETE CASCADE
        );
    ''')
    db.commit()
    db.close()

# Initialize database on startup
init_db()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def count_words(text):
    """Count words in text, handling HTML tags and extra whitespace."""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return len(text.split()) if text else 0

def get_preview(text, word_count=100):
    """Extract first N words from text for preview."""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    words = text.split()
    preview = ' '.join(words[:word_count])
    if len(words) > word_count:
        preview += '...'
    return preview

def extract_chapter_heading(content):
    """Extract main heading from content using Kindle methodology.

    Kindle reads the visual hierarchy of headings in the content file itself,
    preferring the first significant heading found (h1 > h2 > h3 > h4).
    """
    try:
        root = ET.fromstring(content)
        ns = {'html': 'http://www.w3.org/1999/xhtml'}

        # Try with namespace first (XHTML)
        for tag in ['html:h1', 'html:h2', 'html:h3', 'html:h4']:
            for elem in root.findall('.//' + tag, ns):
                text = extract_text(elem).strip()
                if text and len(text) > 0:
                    return text

        # Try without namespace (HTML)
        for tag in ['h1', 'h2', 'h3', 'h4']:
            for elem in root.findall('.//' + tag):
                text = extract_text(elem).strip()
                if text and len(text) > 0:
                    return text
    except:
        pass

    return None

def extract_text(elem):
    """Recursively extract all text from an element."""
    text = elem.text or ''
    for child in elem:
        text += extract_text(child)
        if child.tail:
            text += child.tail
    return text

def get_toc_entries(zip_ref, opf_path, opf_dir):
    """Extract chapter titles from TOC (toc.ncx or nav.xhtml)."""
    toc_map = {}

    # Try all possible toc.ncx locations
    toc_ncx_paths = [
        'toc.ncx',
        'OEBPS/toc.ncx',
        'content/toc.ncx',
        opf_dir + '/toc.ncx' if opf_dir else None
    ]

    # Also search for any .ncx files
    for name in zip_ref.namelist():
        if name.endswith('.ncx') and 'toc' in name.lower():
            toc_ncx_paths.append(name)

    # Try toc.ncx files
    for toc_path in toc_ncx_paths:
        if not toc_path or toc_path not in zip_ref.namelist():
            continue

        try:
            with zip_ref.open(toc_path) as f:
                toc_root = ET.fromstring(f.read())
                ns = {'ncx': 'http://www.daisy.org/z3986/2005/ncx/'}

                for nav_point in toc_root.findall('.//ncx:navPoint', ns):
                    label_elem = nav_point.find('ncx:navLabel/ncx:text', ns)
                    content_elem = nav_point.find('ncx:content', ns)

                    if label_elem is not None and content_elem is not None:
                        title = label_elem.text or 'Untitled'
                        src = content_elem.get('src', '')
                        # Normalize the path - remove directory prefix
                        src_file = src.split('#')[0].split('/')[-1]
                        toc_map[src_file] = title
                        # Also store with full relative path
                        toc_map[src.split('#')[0]] = title

                if toc_map:
                    return toc_map
        except:
            continue

    # Try nav.xhtml (EPUB 3.0 standard)
    nav_paths = ['nav.xhtml', 'OEBPS/nav.xhtml', 'content/nav.xhtml']

    # Search for nav.xhtml in any location
    for name in zip_ref.namelist():
        if name.endswith('nav.xhtml'):
            nav_paths.append(name)

    for nav_path in nav_paths:
        if nav_path not in zip_ref.namelist():
            continue

        try:
            with zip_ref.open(nav_path) as f:
                nav_root = ET.fromstring(f.read())
                ns = {'xhtml': 'http://www.w3.org/1999/xhtml'}

                # Find all nav elements with epub:type="toc"
                for nav in nav_root.findall('.//xhtml:nav', ns):
                    nav_type = nav.get('{http://www.idpf.org/2007/epub}type', '')
                    if 'toc' not in nav_type and nav != nav_root.find('.//xhtml:nav', ns):
                        continue

                    # Find the ol inside this nav
                    ol = nav.find('.//xhtml:ol', ns)
                    if ol is not None:
                        # Process first-level li items (chapters)
                        for li in ol.findall('xhtml:li', ns):
                            a_elem = li.find('xhtml:a', ns)
                            if a_elem is not None:
                                href = a_elem.get('href', '')
                                # Get all text from the link and nested elements
                                text_parts = []
                                text_parts.append(a_elem.text or '')
                                for elem in a_elem:
                                    if elem.text:
                                        text_parts.append(elem.text)
                                    if elem.tail:
                                        text_parts.append(elem.tail)
                                if a_elem.tail:
                                    text_parts.append(a_elem.tail)

                                text = ''.join(text_parts).strip()

                                if href and text:
                                    # Normalize paths
                                    href_file = href.split('#')[0].split('/')[-1]
                                    toc_map[href_file] = text
                                    toc_map[href.split('#')[0]] = text

                if toc_map:
                    return toc_map
        except:
            continue

    return toc_map

def parse_epub(file_path):
    """Parse EPUB file and extract chapters with word counts using TOC."""
    chapters = []

    try:
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            # Find and parse OPF file (package document)
            opf_path = None
            try:
                with zip_ref.open('META-INF/container.xml') as f:
                    root = ET.fromstring(f.read())
                    ns = {'container': 'urn:oasis:names:tc:opendocument:xmlns:container'}
                    opf_element = root.find('.//container:rootfile', ns)
                    if opf_element is not None:
                        opf_path = opf_element.get('full-path')
            except:
                pass

            if not opf_path:
                # Fallback: look for .opf file
                for name in zip_ref.namelist():
                    if name.endswith('.opf'):
                        opf_path = name
                        break

            if not opf_path:
                return {'error': 'Could not find EPUB manifest'}

            opf_dir = str(Path(opf_path).parent)
            if opf_dir == '.':
                opf_dir = ''

            # Get TOC entries
            toc_map = get_toc_entries(zip_ref, opf_path, opf_dir)

            # Parse OPF to get spine (reading order)
            with zip_ref.open(opf_path) as f:
                opf_content = f.read().decode('utf-8', errors='ignore')
                opf_root = ET.fromstring(opf_content)

                ns = {'opf': 'http://www.idpf.org/2007/opf'}
                spine = opf_root.find('.//opf:spine', ns)

                if spine is None:
                    return {'error': 'Could not find EPUB spine'}

                # Extract manifest for ID to href mapping
                manifest = {}
                manifest_elem = opf_root.find('.//opf:manifest', ns)
                if manifest_elem is not None:
                    for item in manifest_elem.findall('opf:item', ns):
                        manifest[item.get('id')] = item.get('href')

                # Process spine items
                chapter_num = 0
                for itemref in spine.findall('opf:itemref', ns):
                    idref = itemref.get('idref')
                    if idref in manifest:
                        href = manifest[idref]

                        # Build full path
                        if opf_dir:
                            content_path = f"{opf_dir}/{href}".replace('//', '/')
                        else:
                            content_path = href

                        try:
                            with zip_ref.open(content_path) as f:
                                content = f.read().decode('utf-8', errors='ignore')
                                word_count = count_words(content)

                                # Kindle methodology: extract heading from content hierarchy
                                title = extract_chapter_heading(content)

                                # Fall back to TOC if no heading found
                                if not title:
                                    for key in [href, content_path, href.split('/')[-1], content_path.split('/')[-1]]:
                                        if key in toc_map:
                                            title = toc_map[key]
                                            break

                                if not title:
                                    title = f'Section {chapter_num + 1}'

                                # Only include chapters with more than MIN_CHAPTER_WORDS
                                if word_count >= MIN_CHAPTER_WORDS:
                                    chapter_num += 1
                                    preview = get_preview(content, 100)
                                    chapters.append({
                                        'number': chapter_num,
                                        'title': title,
                                        'word_count': word_count,
                                        'preview': preview
                                    })
                        except Exception as e:
                            pass

        if not chapters:
            return {'error': f'No chapters found with more than {MIN_CHAPTER_WORDS} words'}

        return {
            'success': True,
            'chapters': chapters,
            'total_chapters': len(chapters),
            'total_words': sum(c['word_count'] for c in chapters)
        }

    except Exception as e:
        return {'error': str(e)}

@app.route('/')
def index():
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EPUB Library</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; }
        .header h1 { font-size: 28px; margin-bottom: 5px; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; }
        .tab-btn { background: white; border: 2px solid #ddd; padding: 12px 24px; border-radius: 8px; cursor: pointer; font-weight: 600; transition: all 0.3s; }
        .tab-btn.active { background: #667eea; color: white; border-color: #667eea; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .upload-box { background: white; border: 2px dashed #667eea; border-radius: 8px; padding: 40px; text-align: center; cursor: pointer; }
        .upload-box:hover { border-color: #764ba2; }
        .upload-box input { display: none; }
        .upload-btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 12px 30px; border-radius: 6px; cursor: pointer; font-weight: 600; margin-top: 15px; }
        .upload-btn:hover { transform: translateY(-2px); }
        .library { background: white; border-radius: 8px; padding: 20px; }
        .epub-card { background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 8px; padding: 15px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: center; cursor: pointer; transition: all 0.3s; }
        .epub-card:hover { background: #f0f2ff; border-color: #667eea; }
        .epub-info h3 { color: #333; margin-bottom: 5px; }
        .epub-meta { color: #666; font-size: 14px; }
        .epub-actions { display: flex; gap: 10px; }
        .btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; transition: all 0.3s; }
        .btn-edit { background: #667eea; color: white; }
        .btn-delete { background: #dc3545; color: white; }
        .epub-title { margin: 0; cursor: text; }
        .epub-title-input { font-size: 24px; font-weight: 700; padding: 8px; border: 2px solid #667eea; border-radius: 4px; width: 100%; }
        .epub-title-input:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1); }
        .btn-delete-chapter { background: #dc3545; color: white; padding: 6px 12px; font-size: 12px; }
        .btn-back { background: #6c757d; color: white; }
        .btn:hover { transform: translateY(-2px); }
        .chapter-actions { display: flex; gap: 8px; }
        .detail-view { background: white; border-radius: 8px; padding: 20px; }
        .chapter-item { background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 8px; padding: 15px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; position: relative; }
        .chapter-item:hover { background: #f0f2ff; border-color: #667eea; }
        .chapter-info { flex: 1; position: relative; }
        .chapter-title { color: #333; font-weight: 600; margin-bottom: 5px; }
        .chapter-words { color: #667eea; font-weight: 600; }
        .chapter-edit { flex: 1; }
        .chapter-edit input { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        .tooltip { position: absolute; bottom: 100%; left: 0; background: #333; color: white; padding: 12px; border-radius: 6px; font-size: 13px; max-width: 350px; width: 350px; z-index: 1000; margin-bottom: 8px; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.15); line-height: 1.5; word-wrap: break-word; white-space: normal; }
        .tooltip::after { content: ''; position: absolute; top: 100%; left: 12px; border: 6px solid transparent; border-top-color: #333; }
        .chapter-item:hover .tooltip { display: block; }
        .message { padding: 15px; border-radius: 8px; margin-bottom: 20px; }
        .success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .loading { text-align: center; padding: 40px; color: #667eea; font-weight: 600; }
        .empty { text-align: center; padding: 40px; color: #999; }
    </style>
</head>
<body>
    <div class="header">
        <h1>📚 EPUB Library</h1>
        <p>Manage and organize your EPUB files</p>
    </div>

    <div class="container">
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('library')">📖 Library</button>
            <button class="tab-btn" onclick="switchTab('upload')">➕ Upload</button>
        </div>

        <div id="library" class="tab-content active">
            <div class="library">
                <h2 style="margin-bottom: 20px;">Your EPUBs</h2>
                <div id="epubList"></div>
            </div>
        </div>

        <div id="upload" class="tab-content">
            <div class="upload-box" id="uploadBox">
                <h3>📤 Upload EPUB File</h3>
                <p>Drag and drop your EPUB file or click to browse</p>
                <button class="upload-btn">Choose File</button>
                <input type="file" id="fileInput" accept=".epub" />
            </div>
            <div id="uploadMessage"></div>
        </div>
    </div>

    <script>
        let currentEpubId = null;

        function switchTab(tabName) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(tabName).classList.add('active');
            event.target.classList.add('active');

            if (tabName === 'library') {
                loadLibrary();
            }
        }

        function loadLibrary() {
            fetch('/epubs')
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('epubList');
                    if (!data.epubs || data.epubs.length === 0) {
                        list.innerHTML = '<div class="empty">No EPUBs uploaded yet. Go to Upload tab to add one.</div>';
                        return;
                    }

                    list.innerHTML = data.epubs.map(epub => `
                        <div class="epub-card" onclick="viewEpub(${epub.id})">
                            <div class="epub-info">
                                <h3 class="epub-title" data-id="${epub.id}" data-original="${epub.filename}">${epub.filename}</h3>
                                <div class="epub-meta">${epub.total_chapters} chapters • ${epub.total_words.toLocaleString()} words</div>
                            </div>
                            <div class="epub-actions">
                                <button class="btn btn-edit" onclick="editEpubTitle(event, ${epub.id})">Edit</button>
                                <button class="btn btn-delete" onclick="deleteEpub(event, ${epub.id})">Delete</button>
                            </div>
                        </div>
                    `).join('');
                });
        }

        function viewEpub(epubId) {
            if (event && event.target.classList.contains('btn-delete')) return;

            fetch(`/epubs/${epubId}`)
                .then(r => r.json())
                .then(data => {
                    currentEpubId = epubId;
                    const list = document.getElementById('epubList');
                    list.innerHTML = `
                        <button class="btn btn-back" onclick="loadLibrary()">← Back to Library</button>
                        <div style="margin-top: 20px;">
                            <h2>${data.epub.filename}</h2>
                            <div style="margin: 15px 0; color: #666;">
                                ${data.chapters.length} chapters • ${data.epub.total_words.toLocaleString()} words
                            </div>
                            <div id="chapterList" style="margin-top: 20px;">
                                ${data.chapters.map(ch => `
                                    <div class="chapter-item">
                                        <div class="chapter-info">
                                            <div class="tooltip">${ch.preview || 'No preview available'}</div>
                                            <div class="chapter-title">${ch.number}. <span class="editable-title" data-id="${ch.id}">${ch.title}</span></div>
                                            <div class="chapter-words">${ch.word_count.toLocaleString()} words</div>
                                        </div>
                                        <div class="chapter-actions">
                                            <button class="btn btn-edit" onclick="editChapter(event, ${ch.id}, '${ch.title.replace(/'/g, "\\'")}')">Edit</button>
                                            <button class="btn btn-delete-chapter" onclick="deleteChapter(event, ${ch.id})">Delete</button>
                                        </div>
                                    </div>
                                `).join('')}
                            </div>
                        </div>
                    `;
                });
        }

        function editChapter(e, chapterId, currentTitle) {
            e.stopPropagation();
            const titleEl = document.querySelector(`[data-id="${chapterId}"]`);
            const input = document.createElement('input');
            input.type = 'text';
            input.value = currentTitle;
            input.onblur = () => saveChapter(chapterId, input.value);
            input.onkeypress = (ev) => {
                if (ev.key === 'Enter') saveChapter(chapterId, input.value);
            };
            titleEl.replaceWith(input);
            input.focus();
            input.select();
        }

        function saveChapter(chapterId, newTitle) {
            fetch(`/epubs/${currentEpubId}/chapters/${chapterId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title: newTitle})
            })
            .then(r => r.json())
            .then(() => {
                const span = document.createElement('span');
                span.className = 'editable-title';
                span.setAttribute('data-id', chapterId);
                span.textContent = newTitle;
                document.querySelector(`input[value="${newTitle}"]`).replaceWith(span);
            });
        }

        function deleteChapter(e, chapterId) {
            e.stopPropagation();
            if (!confirm('Delete this chapter? This action cannot be undone.')) return;

            fetch(`/epubs/${currentEpubId}/chapters/${chapterId}`, {
                method: 'DELETE'
            })
            .then(r => r.json())
            .then(data => {
                if (data && data.success) {
                    viewEpub(currentEpubId);
                } else {
                    alert('Error: ' + (data?.error || 'Unknown error'));
                }
            })
            .catch(err => {
                console.error('Delete failed:', err);
                alert('Error deleting chapter: ' + err.message);
            });
        }

        function editEpubTitle(e, epubId) {
            e.stopPropagation();
            const titleElement = document.querySelector(`.epub-title[data-id="${epubId}"]`);
            const currentTitle = titleElement.textContent;
            const originalTitle = titleElement.dataset.original;

            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'epub-title-input';
            input.value = currentTitle;

            titleElement.replaceWith(input);
            input.focus();
            input.select();

            function saveTitle() {
                const newTitle = input.value.trim();
                if (!newTitle) {
                    input.value = currentTitle;
                    restoreTitle();
                    return;
                }

                fetch(`/epubs/${epubId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: newTitle })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        const newTitleElement = document.createElement('h3');
                        newTitleElement.className = 'epub-title';
                        newTitleElement.dataset.id = epubId;
                        newTitleElement.dataset.original = originalTitle;
                        newTitleElement.textContent = data.title;
                        input.replaceWith(newTitleElement);
                        loadLibrary();
                    } else {
                        alert('Error: ' + (data.error || 'Could not save title'));
                        restoreTitle();
                    }
                })
                .catch(err => {
                    console.error('Save failed:', err);
                    alert('Error saving title: ' + err.message);
                    restoreTitle();
                });
            }

            function restoreTitle() {
                const titleElement = document.createElement('h3');
                titleElement.className = 'epub-title';
                titleElement.dataset.id = epubId;
                titleElement.dataset.original = originalTitle;
                titleElement.textContent = currentTitle;
                input.replaceWith(titleElement);
            }

            input.addEventListener('blur', saveTitle);
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') saveTitle();
                if (e.key === 'Escape') restoreTitle();
            });
        }

        function deleteEpub(e, epubId) {
            e.stopPropagation();
            if (!confirm('Delete this EPUB?')) return;

            fetch(`/epubs/${epubId}`, {method: 'DELETE'})
                .then(r => r.json())
                .then(() => loadLibrary());
        }

        // Upload handling
        const uploadBox = document.getElementById('uploadBox');
        const fileInput = document.getElementById('fileInput');
        const uploadMsg = document.getElementById('uploadMessage');

        uploadBox.addEventListener('dragover', e => {
            e.preventDefault();
            uploadBox.style.borderColor = '#764ba2';
            uploadBox.style.background = '#f0f2ff';
        });

        uploadBox.addEventListener('dragleave', () => {
            uploadBox.style.borderColor = '#667eea';
            uploadBox.style.background = 'white';
        });

        uploadBox.addEventListener('drop', e => {
            e.preventDefault();
            uploadBox.style.borderColor = '#667eea';
            uploadBox.style.background = 'white';
            handleFiles(e.dataTransfer.files);
        });

        uploadBox.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', e => handleFiles(e.target.files));

        function handleFiles(files) {
            if (!files[0]?.name.toLowerCase().endsWith('.epub')) {
                uploadMsg.innerHTML = '<div class="message error">Please upload an EPUB file</div>';
                return;
            }

            uploadMsg.innerHTML = '<div class="message" style="background: #e7f3ff; color: #0066cc;">Processing...</div>';

            const form = new FormData();
            form.append('file', files[0]);

            fetch('/upload', {method: 'POST', body: form})
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        uploadMsg.innerHTML = `<div class="message error">${data.error}</div>`;
                    } else {
                        uploadMsg.innerHTML = `<div class="message success">✓ Uploaded successfully!</div>`;
                        fileInput.value = '';
                        setTimeout(() => {
                            document.querySelectorAll('.tab-btn')[0].click();
                        }, 500);
                    }
                })
                .catch(err => {
                    uploadMsg.innerHTML = `<div class="message error">Upload failed: ${err.message}</div>`;
                });
        }

        // Load library on start
        loadLibrary();
    </script>
</body>
</html>'''

@app.route('/epubs', methods=['GET'])
def list_epubs():
    """Get list of all uploaded EPUBs."""
    db = get_db()
    epubs = db.execute('SELECT * FROM epubs ORDER BY created_at DESC').fetchall()
    db.close()

    return jsonify({
        'success': True,
        'epubs': [dict(e) for e in epubs]
    })

@app.route('/epubs/<int:epub_id>', methods=['GET'])
def get_epub(epub_id):
    """Get details of a specific EPUB with all chapters."""
    db = get_db()
    epub = db.execute('SELECT * FROM epubs WHERE id = ?', (epub_id,)).fetchone()

    if not epub:
        db.close()
        return jsonify({'error': 'EPUB not found'}), 404

    chapters = db.execute(
        'SELECT * FROM chapters WHERE epub_id = ? ORDER BY number',
        (epub_id,)
    ).fetchall()
    db.close()

    return jsonify({
        'success': True,
        'epub': dict(epub),
        'chapters': [dict(c) for c in chapters]
    })

@app.route('/epubs/<int:epub_id>', methods=['PUT'])
def update_epub(epub_id):
    """Update an EPUB's title."""
    data = request.get_json()

    if not data or 'title' not in data:
        return jsonify({'error': 'Title is required'}), 400

    db = get_db()
    db.execute(
        'UPDATE epubs SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (data['title'], epub_id)
    )
    db.commit()

    epub = db.execute('SELECT * FROM epubs WHERE id = ?', (epub_id,)).fetchone()
    db.close()

    if not epub:
        return jsonify({'error': 'EPUB not found'}), 404

    return jsonify({
        'success': True,
        'title': data['title'],
        'epub': dict(epub)
    })

@app.route('/epubs/<int:epub_id>/chapters/<int:chapter_id>', methods=['PUT'])
def update_chapter(epub_id, chapter_id):
    """Update a chapter title."""
    data = request.get_json()

    if not data or 'title' not in data:
        return jsonify({'error': 'Title is required'}), 400

    db = get_db()
    db.execute(
        'UPDATE chapters SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND epub_id = ?',
        (data['title'], chapter_id, epub_id)
    )
    db.commit()

    chapter = db.execute(
        'SELECT * FROM chapters WHERE id = ?', (chapter_id,)
    ).fetchone()
    db.close()

    return jsonify({
        'success': True,
        'chapter': dict(chapter)
    })

@app.route('/epubs/<int:epub_id>/chapters/<int:chapter_id>', methods=['DELETE'])
def delete_chapter(epub_id, chapter_id):
    """Delete a chapter."""
    db = get_db()

    cursor = db.execute(
        'SELECT id FROM chapters WHERE id = ? AND epub_id = ?',
        (chapter_id, epub_id)
    )

    if not cursor.fetchone():
        db.close()
        return jsonify({'error': 'Chapter not found'}), 404

    db.execute('DELETE FROM chapters WHERE id = ? AND epub_id = ?', (chapter_id, epub_id))
    db.commit()
    db.close()

    return jsonify({'success': True, 'message': 'Chapter deleted'})

@app.route('/epubs/<int:epub_id>', methods=['DELETE'])
def delete_epub(epub_id):
    """Delete an EPUB and all its chapters."""
    db = get_db()

    # Check if EPUB exists
    epub = db.execute('SELECT * FROM epubs WHERE id = ?', (epub_id,)).fetchone()
    if not epub:
        db.close()
        return jsonify({'error': 'EPUB not found'}), 404

    # Delete chapters and EPUB (cascading delete)
    db.execute('DELETE FROM chapters WHERE epub_id = ?', (epub_id,))
    db.execute('DELETE FROM epubs WHERE id = ?', (epub_id,))
    db.commit()
    db.close()

    return jsonify({'success': True, 'message': 'EPUB deleted'})

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'})

    if not allowed_file(file.filename):
        return jsonify({'error': 'Only EPUB files are supported'})

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        result = parse_epub(filepath)

        if 'error' in result:
            return jsonify(result), 400

        # Save to database
        db = get_db()
        cursor = db.execute(
            'INSERT INTO epubs (filename, title, total_chapters, total_words) VALUES (?, ?, ?, ?)',
            (file.filename, file.filename, result['total_chapters'], result['total_words'])
        )
        epub_id = cursor.lastrowid

        # Insert chapters
        for chapter in result['chapters']:
            db.execute(
                'INSERT INTO chapters (epub_id, number, title, word_count, preview) VALUES (?, ?, ?, ?, ?)',
                (epub_id, chapter['number'], chapter['title'], chapter['word_count'], chapter.get('preview', ''))
            )

        db.commit()
        db.close()

        return jsonify({
            'success': True,
            'id': epub_id,
            'chapters': result['chapters'],
            'total_chapters': result['total_chapters'],
            'total_words': result['total_words']
        })
    finally:
        # Clean up uploaded file
        try:
            os.remove(filepath)
        except:
            pass

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)