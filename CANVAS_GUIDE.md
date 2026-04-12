# Loom Canvas — Developer Guide

This guide explains how to build interactive canvas pages for A Shadow Loom. Pass this to any AI agent that has a canvas-enabled conversation.

## What is the Canvas?

The canvas is a live website (HTML/CSS/JS) rendered in an iframe within the Loom UI. When you write files to the `canvas/` directory in your project, they appear in the user's browser immediately. The entry point is always `canvas/index.html`.

The canvas appears as a glowing meta-root node in the conversation's tree view. Clicking it opens a fullscreen view with the chat bar still visible, so the user can keep talking to you while viewing the canvas.

## File Structure

```
canvas/
  index.html          # Entry point — always loaded by the iframe
  style.css           # Optional: your styles
  app.js              # Optional: your scripts
  triggers/           # Prompt templates for SDK interactions
    analyze.md
    summarize.md
  CLAUDE.md           # Auto-generated instructions (you can read but don't need to edit)
```

## Basic Canvas

Just write HTML to `canvas/index.html`:

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>My Canvas</title>
    <style>
        body { font-family: sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }
    </style>
</head>
<body>
    <h1>Dashboard</h1>
    <div id="content">Loading...</div>
</body>
</html>
```

Every time you write or edit files in `canvas/`, the iframe auto-refreshes via WebSocket.

## Canvas SDK — Making It Interactive

Include the SDK to let the canvas page send messages back to Loom, upload files, and trigger AI generation:

```html
<script src="/static/canvas-sdk.js"></script>
```

This gives you the global `Loom` object.

### Loom.send(prompt, opts?)

Send a chat message and trigger AI generation. The AI response will appear as a new branch in the conversation.

```javascript
// Simple prompt
Loom.send("Update the chart with latest data");

// With options
Loom.send("Analyze this image", {
    imagePaths: ["/uploads/photo.png"],  // attach files
    parentId: 42                          // branch from specific message
});
```

### Loom.upload(file)

Upload a File object (from drag-drop, file input, etc). Returns the server path you can reference in messages.

```javascript
const input = document.getElementById('file-input');
input.addEventListener('change', async () => {
    const result = await Loom.upload(input.files[0]);
    // result = { path: "C:/uploads/file.csv", url: "/uploads/file.csv", is_image: false }
});
```

### Loom.uploadAndSend(file, prompt)

Convenience: upload a file and send a message referencing it in one call.

```javascript
Loom.uploadAndSend(droppedFile, "Parse this CSV and build a chart on the canvas");
```

### Loom.loadTrigger(name, vars?)

Load a prompt template from `canvas/triggers/{name}.md` and fill in `{{variable}}` placeholders.

```javascript
// canvas/triggers/analyze.md contains:
// "Analyze {{filename}} and update canvas/index.html with a visualization of the data."

const prompt = await Loom.loadTrigger('analyze', { filename: 'sales.csv' });
Loom.send(prompt);
```

### Loom.dropZone(element, opts?)

Turn any element into a drag-and-drop zone. When files are dropped, they're uploaded and a prompt is sent automatically.

```javascript
const zone = document.getElementById('drop-zone');

// Using a trigger template
Loom.dropZone(zone, { trigger: 'analyze' });

// Using a static prompt
Loom.dropZone(zone, { prompt: 'Process this file and update the canvas' });

// With a callback
Loom.dropZone(zone, {
    trigger: 'analyze',
    onDrop: (files) => {
        zone.textContent = `Uploading ${files.length} file(s)...`;
    }
});
```

The drop zone automatically:
1. Uploads all dropped files
2. Loads the trigger template (or uses the static prompt)
3. Fills in `{{filename}}`, `{{filenames}}`, and `{{count}}`
4. Sends the message with file attachments
5. AI generates a response and can write back to canvas

### Loom.getConvId()

Get the current conversation ID (useful for direct API calls).

```javascript
const { convId } = await Loom.getConvId();
```

### Loom.on(event, handler)

Listen for events from Loom.

```javascript
Loom.on('message-sent', (data) => {
    console.log('Message created:', data.id);
});
```

## Trigger Templates

Trigger templates are markdown files in `canvas/triggers/`. They're plain text with `{{variable}}` placeholders:

**`canvas/triggers/analyze.md`**
```
Analyze the uploaded file "{{filename}}" and update the canvas:

1. Parse the file contents
2. Extract key metrics
3. Write an updated canvas/index.html with:
   - A summary section showing the metrics
   - A table of the raw data
   - Use a dark theme matching Loom's aesthetic (#1a1a2e background, #0ff and #bf00ff accents)
```

**`canvas/triggers/summarize.md`**
```
Read through {{filename}} and create a visual summary on the canvas.
Focus on the most important {{count}} points. Write results to canvas/index.html.
```

## Example: File Analysis Dashboard

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Analysis Dashboard</title>
    <script src="/static/canvas-sdk.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; }
        body { font-family: 'Space Grotesk', sans-serif; background: #0a0a19; color: #ddd; padding: 24px; }
        h1 { color: #0ff; margin-bottom: 16px; }
        #drop-zone {
            border: 2px dashed rgba(0, 255, 255, 0.3);
            border-radius: 12px;
            padding: 48px;
            text-align: center;
            color: #888;
            transition: 0.2s;
            cursor: pointer;
        }
        #drop-zone.loom-dragover {
            border-color: #0ff;
            background: rgba(0, 255, 255, 0.05);
            color: #0ff;
        }
        #results { margin-top: 24px; }
        button {
            background: rgba(191, 0, 255, 0.2);
            border: 1px solid rgba(191, 0, 255, 0.5);
            color: #ddd;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            margin-top: 12px;
        }
        button:hover { background: rgba(191, 0, 255, 0.35); }
    </style>
</head>
<body>
    <h1>Analysis Dashboard</h1>

    <div id="drop-zone">
        Drop a file here to analyze it
    </div>

    <button onclick="Loom.send('Refresh the dashboard with the latest state')">
        Refresh
    </button>

    <div id="results">
        <!-- AI will populate this section -->
    </div>

    <script>
        const zone = document.getElementById('drop-zone');
        Loom.dropZone(zone, {
            trigger: 'analyze',
            onDrop: (files) => {
                zone.textContent = `Processing ${files[0].name}...`;
            }
        });
    </script>
</body>
</html>
```

## Tips

- **Dark theme**: Match Loom's aesthetic — `#0a0a19` background, `#0ff` (cyan) and `#bf00ff` (purple) accents
- **Progressive building**: Start with static HTML, add SDK interactivity in later turns
- **Self-updating**: Your AI responses can rewrite `canvas/index.html` — the iframe auto-refreshes
- **State in the canvas**: Use localStorage, hidden elements, or data attributes to persist state across refreshes
- **Multiple pages**: You can have multiple HTML files, but `index.html` is always the iframe entry point. Use JS to swap content rather than separate pages
- **The canvas is sandboxed**: `allow-scripts allow-same-origin` — you can run JS and make same-origin API calls, but no popups or top-level navigation
