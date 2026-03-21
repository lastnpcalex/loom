/**
 * Loom — Interactive canvas tree visualization
 * ComfyUI-inspired: pan, zoom, drag, algorithmic layout
 * Branch naming: alpha/beta/gamma (vertical forks), 1/2/3 (horizontal position)
 * Click pill → expand to chat. Hover → 6-word summary.
 */

const GREEK = ['alpha','beta','gamma','delta','epsilon','zeta','eta','theta','iota','kappa','lambda','mu','nu','xi','omicron','pi','rho','sigma','tau','upsilon','phi','chi','psi','omega'];

const TREE = {
    nodeWidth: 260,
    nodeMinHeight: 40,
    rootNodeWidth: 300,
    gapX: 40,
    gapY: 80,
    connectorColor: '#30363d',
    connectorActiveColor: '#58a6ff',
    // Canvas state
    panX: 0,
    panY: 0,
    zoom: 1,
    isPanning: false,
    panStartX: 0,
    panStartY: 0,
    isDragging: false,
    dragNodeId: null,
    dragOffsetX: 0,
    dragOffsetY: 0,
    manualPositions: {},  // id -> {x, y} for manually dragged nodes
};

// ── Canvas Pan/Zoom ──

function initCanvas() {
    const canvas = document.getElementById('tree-canvas');
    if (!canvas) return;

    // Pan with middle-click or right-click drag, or left-click on empty space
    canvas.addEventListener('mousedown', (e) => {
        // Middle click or right click to pan
        if (e.button === 1 || e.button === 2) {
            e.preventDefault();
            TREE.isPanning = true;
            TREE.panStartX = e.clientX - TREE.panX;
            TREE.panStartY = e.clientY - TREE.panY;
            canvas.style.cursor = 'grabbing';
            return;
        }
        // Left click on empty space to pan
        if (e.target === canvas || e.target.id === 'tree-nodes') {
            TREE.isPanning = true;
            TREE.panStartX = e.clientX - TREE.panX;
            TREE.panStartY = e.clientY - TREE.panY;
            canvas.style.cursor = 'grabbing';
        }
    });

    canvas.addEventListener('mousemove', (e) => {
        if (TREE.isPanning) {
            TREE.panX = e.clientX - TREE.panStartX;
            TREE.panY = e.clientY - TREE.panStartY;
            applyTransform();
        }
    });

    canvas.addEventListener('mouseup', () => {
        TREE.isPanning = false;
        canvas.style.cursor = '';
    });

    canvas.addEventListener('mouseleave', () => {
        TREE.isPanning = false;
        canvas.style.cursor = '';
    });

    // Zoom with scroll wheel
    canvas.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? 0.9 : 1.1;
        const newZoom = Math.max(0.2, Math.min(3, TREE.zoom * delta));

        // Zoom toward mouse position
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        TREE.panX = mx - (mx - TREE.panX) * (newZoom / TREE.zoom);
        TREE.panY = my - (my - TREE.panY) * (newZoom / TREE.zoom);
        TREE.zoom = newZoom;
        applyTransform();
    }, { passive: false });

    // Prevent context menu on canvas
    canvas.addEventListener('contextmenu', (e) => e.preventDefault());
}

function applyTransform() {
    const inner = document.getElementById('tree-nodes');
    if (inner) {
        inner.style.transform = `translate(${TREE.panX}px, ${TREE.panY}px) scale(${TREE.zoom})`;
        inner.style.transformOrigin = '0 0';
    }
}

function resetCanvasView() {
    TREE.panX = 40;
    TREE.panY = 40;
    TREE.zoom = 1;
    applyTransform();
}

// ── Branch Naming ──

function getBranchLabel(depth) {
    return depth < GREEK.length ? GREEK[depth] : `branch${depth}`;
}

function computeBranchNames(roots, nodeMap, childrenMap) {
    const names = {};  // nodeId -> label string

    function walk(nodeId, pathPrefix, position, forkDepth) {
        const label = pathPrefix ? `${pathPrefix}.${position}` : `${position}`;
        names[nodeId] = label;

        const children = childrenMap[nodeId] || [];
        if (children.length === 1) {
            walk(children[0], pathPrefix, position + 1, forkDepth);
        } else if (children.length > 1) {
            for (let i = 0; i < children.length; i++) {
                const branchName = getBranchLabel(i);
                const newPrefix = pathPrefix ? `${pathPrefix}.${position}.${branchName}` : `${position}.${branchName}`;
                walk(children[i], newPrefix.replace(/\.\d+$/, ''), 1, forkDepth + 1);
            }
        }
    }

    for (let i = 0; i < roots.length; i++) {
        const rootLabel = roots.length > 1 ? getBranchLabel(i) : '';
        walk(roots[i], rootLabel, 1, 0);
    }

    return names;
}

// ── Summary ──

function summarize(text, wordCount = 6) {
    if (!text) return '...';
    const words = text.replace(/\n/g, ' ').replace(/\s+/g, ' ').trim().split(' ');
    if (words.length <= wordCount) return words.join(' ');
    return words.slice(0, wordCount).join(' ') + '...';
}

function getNodeSummary(data) {
    // Prefer Gemma-generated summary, fall back to word truncation
    if (data.summary) return data.summary;
    return summarize(data.preview, 10);
}

// ── Render ──

async function renderTree() {
    const container = document.getElementById('tree-nodes');
    if (!container) return;

    // Always re-fetch tree data to pick up new Gemma summaries
    if (State.currentConvId) {
        try {
            State.treeData = await API.get(`/api/conversations/${State.currentConvId}/tree`);
        } catch (e) {
            console.error('Failed to refresh tree data:', e);
        }
    }

    if (!State.treeData || State.treeData.length === 0) {
        container.innerHTML = '<div style="color:var(--text-muted);padding:60px;text-align:center;font-size:14px;">No messages yet. Start writing to build the tree.</div>';
        return;
    }

    // Build adjacency
    const nodeMap = {};
    const childrenMap = {};
    const roots = [];

    for (const n of State.treeData) {
        nodeMap[n.id] = n;
        childrenMap[n.id] = [];
    }
    for (const n of State.treeData) {
        if (n.parent_id && nodeMap[n.parent_id]) {
            childrenMap[n.parent_id].push(n.id);
        } else {
            roots.push(n.id);
        }
    }

    // Compute names
    const branchNames = computeBranchNames(roots, nodeMap, childrenMap);

    // Layout
    const layout = computeLayout(roots, nodeMap, childrenMap, branchNames);

    // Clear
    container.innerHTML = '';
    container.style.position = 'relative';

    // SVG for connectors
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.style.position = 'absolute';
    svg.style.top = '0';
    svg.style.left = '0';
    svg.style.width = (layout.totalWidth + 100) + 'px';
    svg.style.height = (layout.totalHeight + 100) + 'px';
    svg.style.pointerEvents = 'none';
    svg.style.overflow = 'visible';

    // Create nodes
    const pillEls = {};
    const positions = {};

    for (const node of layout.nodes) {
        const el = createNode(node, branchNames);
        el.style.position = 'absolute';
        el.style.left = node.x + 'px';
        el.style.top = node.y + 'px';
        el.style.width = node.width + 'px';
        container.appendChild(el);
        pillEls[node.data.id] = el;
        positions[node.data.id] = {
            x: node.x, y: node.y,
            width: node.width,
            height: el.offsetHeight || node.height,
        };
    }

    // Measure actual heights after DOM insertion
    requestAnimationFrame(() => {
        for (const node of layout.nodes) {
            const el = pillEls[node.data.id];
            if (el) {
                positions[node.data.id].height = el.offsetHeight;
            }
        }
        // Draw connectors after measuring
        drawConnectors(svg, layout.nodes, positions);
    });

    container.insertBefore(svg, container.firstChild);

    // Initialize canvas if first render
    if (!TREE._initialized) {
        initCanvas();
        resetCanvasView();
        TREE._initialized = true;
    }
}

function createNode(node, branchNames) {
    const data = node.data;
    const isActive = node.isActive;
    const isRoot = !data.parent_id;
    const isForkPoint = node.childCount > 1;
    const label = branchNames[data.id] || '';

    const el = document.createElement('div');
    el.className = `tree-node-card ${data.role}${isActive ? ' active' : ''}${isRoot ? ' root' : ''}`;
    el.dataset.msgId = data.id;

    const roleLabel = data.role === 'user' ? 'You' : data.role === 'assistant' ? getCharacterName() : 'Sys';
    const preview = data.preview || '';
    const summary = getNodeSummary(data);
    const hasSummary = !!data.summary;

    const hasImage = !!data.image_path;

    // Header (always shown)
    const headerHtml = `
        <div class="tree-node-header">
            <span class="tree-node-role">${escapeHtml(roleLabel)}</span>
            ${hasImage ? '<span class="tree-node-img-badge" title="Has image">img</span>' : ''}
            <span class="tree-node-label">${escapeHtml(label)}</span>
            ${isForkPoint ? `<span class="tree-node-fork">${node.childCount} branches</span>` : ''}
            <button class="tree-node-delete-btn" title="Delete branch">&#x2715;</button>
        </div>
    `;

    if (isRoot) {
        el.innerHTML = `
            ${headerHtml}
            <div class="tree-node-text">${escapeHtml(preview)}</div>
        `;
    } else {
        el.innerHTML = `
            ${headerHtml}
            <div class="tree-node-body">
                <div class="tree-node-summary">${escapeHtml(summary)}</div>
                <div class="tree-node-expanded hidden">${escapeHtml(preview)}</div>
                <button class="tree-node-expand-btn" title="Expand / Collapse">&#9662;</button>
            </div>
        `;
    }

    // Expand button: toggle expand/collapse
    const expandBtn = el.querySelector('.tree-node-expand-btn');
    if (expandBtn) {
        expandBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const expanded = el.querySelector('.tree-node-expanded');
            const summaryEl = el.querySelector('.tree-node-summary');
            const isExpanded = !expanded.classList.contains('hidden');
            if (isExpanded) {
                expanded.classList.add('hidden');
                summaryEl.classList.remove('hidden');
                el.classList.remove('expanded');
                expandBtn.innerHTML = '&#9662;'; // ▾ down arrow
            } else {
                summaryEl.classList.add('hidden');
                expanded.classList.remove('hidden');
                el.classList.add('expanded');
                expandBtn.innerHTML = '&#9652;'; // ▴ up arrow
            }
        });
    }

    // Delete button
    const deleteBtn = el.querySelector('.tree-node-delete-btn');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const childInfo = node.childCount > 0 ? ` and ${node.childCount} child branch${node.childCount > 1 ? 'es' : ''}` : '';
            if (!confirm(`Delete this message${childInfo}? This cannot be undone.`)) return;
            try {
                await API.del(`/api/conversations/${State.currentConvId}/messages/${data.id}`);
                showToast('Branch deleted');
                await renderTree();
            } catch (err) {
                showToast('Failed to delete branch', 'error');
            }
        });
    }

    // Click card body: navigate to this branch in chat
    el.addEventListener('click', async (e) => {
        if (TREE.isPanning) return;
        if (e.target.closest('.tree-node-expand-btn')) return;
        if (e.target.closest('.tree-node-delete-btn')) return;
        e.stopPropagation();
        await switchToBranch(data.id);
        switchView('chat');
    });

    return el;
}

function drawConnectors(svg, nodes, positions) {
    svg.innerHTML = '';
    for (const node of nodes) {
        if (node.parentId && positions[node.parentId] && positions[node.data.id]) {
            const parent = positions[node.parentId];
            const child = positions[node.data.id];
            const isActive = node.isActive && nodes.find(n => n.data.id === node.parentId)?.isActive;

            const x1 = parent.x + parent.width;
            const y1 = parent.y + (parent.height || 32) / 2;
            const x2 = child.x;
            const y2 = child.y + (child.height || 32) / 2;
            const midX = x1 + (x2 - x1) / 2;

            const color = isActive ? TREE.connectorActiveColor : TREE.connectorColor;
            const width = isActive ? 2.5 : 1.5;

            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', `M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}`);
            path.setAttribute('fill', 'none');
            path.setAttribute('stroke', color);
            path.setAttribute('stroke-width', width);
            path.setAttribute('stroke-linecap', 'round');
            if (!isActive) path.setAttribute('stroke-dasharray', '4,3');
            svg.appendChild(path);
        }
    }
}

// ── Layout Algorithm ──

function computeLayout(roots, nodeMap, childrenMap, branchNames) {
    const ROOT_W = TREE.rootNodeWidth;
    const NODE_W = TREE.nodeWidth;
    const GAP_X = TREE.gapX;
    const GAP_Y = TREE.gapY;
    const NODE_H = TREE.nodeMinHeight;
    const ROOT_H = 64;

    const nodes = [];
    let maxX = 0;
    let maxY = 0;

    function layoutChain(nodeId, startX, startY, parentId) {
        let x = startX;
        let y = startY;
        let currentId = nodeId;
        let currentParent = parentId;

        while (currentId) {
            const data = nodeMap[currentId];
            const children = childrenMap[currentId] || [];
            const isActive = !!data.is_active;
            const isRoot = !data.parent_id;
            const w = isRoot ? ROOT_W : NODE_W;
            const h = isRoot ? ROOT_H : NODE_H;

            nodes.push({
                data,
                x, y,
                width: w,
                height: h,
                isActive,
                parentId: currentParent,
                childCount: children.length,
            });

            maxX = Math.max(maxX, x + w);
            maxY = Math.max(maxY, y + h);

            if (children.length === 0) {
                return y + h;
            } else if (children.length === 1) {
                currentParent = currentId;
                currentId = children[0];
                x += w + GAP_X;
            } else {
                // Fork
                const forkX = x + w + GAP_X;
                let forkY = y;
                let maxBottom = forkY;

                for (let i = 0; i < children.length; i++) {
                    const bottom = layoutChain(children[i], forkX, forkY, currentId);
                    maxBottom = Math.max(maxBottom, bottom);
                    if (i < children.length - 1) {
                        forkY = bottom + GAP_Y;
                    }
                }
                return maxBottom;
            }
        }
        return y + NODE_H;
    }

    let currentY = 0;
    for (const rootId of roots) {
        const bottom = layoutChain(rootId, 0, currentY, null);
        currentY = bottom + GAP_Y * 3;
    }

    return { nodes, totalWidth: maxX, totalHeight: maxY };
}
