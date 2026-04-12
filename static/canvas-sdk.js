/**
 * Loom Canvas SDK — helper for canvas pages to interact with Loom.
 *
 * Usage in a canvas page:
 *   <script src="/static/canvas-sdk.js"></script>
 *   <script>
 *     // Send a message and trigger AI generation
 *     Loom.send("Analyze this data and update the canvas");
 *
 *     // Upload a file, then send a message referencing it
 *     Loom.uploadAndSend(file, "Process this file and visualize the results");
 *
 *     // Load a prompt template from canvas/triggers/
 *     const prompt = await Loom.loadTrigger('analyze', { filename: 'data.csv' });
 *     Loom.send(prompt);
 *
 *     // Set up drag-drop zone
 *     Loom.dropZone(element, { trigger: 'analyze' });
 *   </script>
 */
(function () {
    'use strict';

    const _pending = {};
    let _reqId = 0;

    // Listen for responses from Loom host
    window.addEventListener('message', (e) => {
        if (!e.data || e.data.source !== 'loom-host') return;
        // Resolve any pending promises
        for (const [id, p] of Object.entries(_pending)) {
            if (e.data.type === p.waitFor) {
                p.resolve(e.data);
                delete _pending[id];
                return;
            }
            if (e.data.type === 'error') {
                p.reject(new Error(e.data.error));
                delete _pending[id];
                return;
            }
        }
        // Fire custom events for unmatched messages
        window.dispatchEvent(new CustomEvent('loom:' + e.data.type, { detail: e.data }));
    });

    function _post(data, waitFor) {
        return new Promise((resolve, reject) => {
            const id = ++_reqId;
            if (waitFor) _pending[id] = { resolve, reject, waitFor };
            window.parent.postMessage({ source: 'loom-canvas', ...data }, '*');
            if (!waitFor) resolve();
            // Timeout after 60s
            setTimeout(() => {
                if (_pending[id]) {
                    delete _pending[id];
                    reject(new Error('Loom request timed out'));
                }
            }, 60000);
        });
    }

    const Loom = {
        /**
         * Send a message to the AI and trigger generation.
         * @param {string} content - The message text
         * @param {object} opts - Optional: { imagePaths: [...], parentId: int }
         * @returns {Promise<{id: number}>} The created message
         */
        send(content, opts = {}) {
            const msg = { type: 'send-message', content };
            if (opts.imagePaths) msg.image_paths = opts.imagePaths;
            if (opts.parentId !== undefined) msg.parent_id = opts.parentId;
            return _post(msg, 'message-sent');
        },

        /**
         * Upload a File object to Loom (direct fetch, same-origin).
         * @param {File} file - The file to upload
         * @returns {Promise<{path: string, url: string, is_image: boolean}>}
         */
        async upload(file) {
            const formData = new FormData();
            formData.append('file', file);
            const resp = await fetch('/api/upload', { method: 'POST', body: formData });
            if (!resp.ok) throw new Error('Upload failed: ' + resp.statusText);
            return resp.json();
        },

        /**
         * Upload a file and send a message referencing it.
         * @param {File} file - The file to upload
         * @param {string} content - The prompt text
         * @returns {Promise<{id: number}>}
         */
        async uploadAndSend(file, content) {
            const uploaded = await this.upload(file);
            return this.send(content, { imagePaths: [uploaded.path] });
        },

        /**
         * Get the current conversation ID.
         * @returns {Promise<{convId: number}>}
         */
        getConvId() {
            return _post({ type: 'get-conv-id' }, 'conv-id');
        },

        /**
         * Load a trigger template from canvas/triggers/{name}.md
         * and interpolate variables: {{varname}} → values[varname]
         * @param {string} name - Trigger filename (without .md)
         * @param {object} vars - Key-value pairs to interpolate
         * @returns {Promise<string>} The interpolated prompt
         */
        async loadTrigger(name, vars = {}) {
            const { convId } = await this.getConvId();
            const resp = await fetch(`/api/canvas/${convId}/triggers/${name}.md`);
            if (!resp.ok) throw new Error(`Trigger "${name}" not found`);
            let text = await resp.text();
            for (const [k, v] of Object.entries(vars)) {
                text = text.replaceAll(`{{${k}}}`, v);
            }
            return text;
        },

        /**
         * Set up a drag-and-drop zone that uploads files and sends a trigger prompt.
         * @param {HTMLElement} el - The drop target element
         * @param {object} opts - { trigger: 'name', prompt: 'fallback prompt', onDrop: fn }
         */
        dropZone(el, opts = {}) {
            el.addEventListener('dragover', (e) => {
                e.preventDefault();
                el.classList.add('loom-dragover');
            });
            el.addEventListener('dragleave', () => {
                el.classList.remove('loom-dragover');
            });
            el.addEventListener('drop', async (e) => {
                e.preventDefault();
                el.classList.remove('loom-dragover');

                const files = Array.from(e.dataTransfer.files);
                if (files.length === 0) return;

                if (opts.onDrop) opts.onDrop(files);

                // Upload all files
                const uploaded = await Promise.all(files.map(f => Loom.upload(f)));
                const paths = uploaded.map(u => u.path);

                // Build prompt
                let prompt;
                if (opts.trigger) {
                    const names = files.map(f => f.name).join(', ');
                    prompt = await Loom.loadTrigger(opts.trigger, {
                        filenames: names,
                        filename: files[0].name,
                        count: String(files.length),
                    });
                } else {
                    prompt = opts.prompt || `Process the attached file(s): ${files.map(f => f.name).join(', ')}`;
                }

                return Loom.send(prompt, { imagePaths: paths });
            });
        },

        /**
         * Listen for Loom events (canvas_updated, message-sent, etc.)
         * @param {string} event - Event name
         * @param {function} handler - Callback
         */
        on(event, handler) {
            window.addEventListener('loom:' + event, (e) => handler(e.detail));
        },
    };

    window.Loom = Loom;
})();
