# EPUB Library Manager

A Flask-based web application for uploading, organizing, and managing EPUB files. View chapter breakdowns with word counts, edit chapter titles, delete chapters, and browse a persistent library of your books.

## Features

- 📚 Upload and manage EPUB files
- 📖 Automatic chapter extraction from table of contents
- 📊 Word count statistics per chapter (300+ word minimum)
- ✏️ Edit chapter titles
- 🗑️ Delete individual chapters
- 💾 Persistent storage with SQLite database
- 🔍 Chapter previews (first 100 words) on hover
- 🎨 Clean, responsive web interface

## Requirements

- Python 3.7+
- Flask 2.3.3
- Werkzeug 2.3.7

## Installation

1. **Clone or download this repository**

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application:**
   ```bash
   python app.py
   ```

   The app will start on `http://localhost:5000`

## Usage

### Upload an EPUB
1. Click the **Upload** tab
2. Drag and drop an EPUB file or click to browse
3. The app will extract chapters and store them in the database

### View Your Library
1. Click the **Library** tab to see all uploaded EPUBs
2. Click an EPUB to view its chapters with word counts
3. Hover over a chapter to see a preview of the first 100 words

### Manage Chapters
- **Edit**: Click the blue "Edit" button to rename a chapter title
- **Delete**: Click the red "Delete" button to remove a chapter
  - Click once to confirm, click again to execute the deletion

### Delete an EPUB
- Click the red "Delete" button next to an EPUB in the library to remove it and all its chapters

## Database

The app uses SQLite (`epubs.db`) for persistent storage. This file is automatically created on first run.

**Schema:**
- `epubs` - Stores uploaded EPUB metadata (filename, title, chapter count, word count)
- `chapters` - Stores individual chapter data with word counts and previews

## API Endpoints

- `GET /epubs` - List all EPUBs
- `GET /epubs/<id>` - Get EPUB details with chapters
- `PUT /epubs/<id>/chapters/<chapter_id>` - Update chapter title
- `DELETE /epubs/<id>/chapters/<chapter_id>` - Delete a chapter
- `DELETE /epubs/<id>` - Delete an EPUB
- `POST /upload` - Upload a new EPUB file

## Architecture

- **Backend**: Flask (Python) with SQLite
- **Frontend**: Single-page application (HTML/CSS/JavaScript)
- **File Parsing**: ZIP extraction and XML parsing for EPUB structure and table of contents

## Notes

- Only chapters with 300+ words are displayed
- Chapter titles are extracted from the book's table of contents
- EPUB 2.0 (toc.ncx) and EPUB 3.0 (nav.xhtml) formats are supported
- Maximum file upload size: 100MB
