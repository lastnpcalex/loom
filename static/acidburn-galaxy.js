/**
 * ACIDBURN GALAXY v2.1
 * 
 * Tiger stripe aesthetic matching the page design
 * Replaces milkyway.jpg with procedural cyberpunk texture
 * 
 * USAGE:
 *   // Generate a static canvas texture
 *   const canvas = AcidburnGalaxy.generate({ width: 2048, height: 1024 });
 *   
 *   // Or with animation
 *   const canvas = AcidburnGalaxy.generate({ animated: true });
 *   AcidburnGalaxy.start(); // start animation loop
 *   AcidburnGalaxy.stop();  // stop animation
 */

var AcidburnGalaxy = (function() {
  'use strict';

  // ═══════════════════════════════════════════════════════════════
  // DEFAULT CONFIGURATION
  // ═══════════════════════════════════════════════════════════════
  
  const DEFAULTS = {
    width: 2048,
    height: 1024,
    
    animated: false,
    frameRate: 20,
    
    colors: {
      background: '#030508',
      purple: '#bf00ff',
      cyan: '#00ffff',
      magenta: '#ff00ff',
      pink: '#ff0099',
    },
    
    // Grid
    grid: {
      enabled: true,
      latLines: 24,
      lonLines: 48,
      lineWidth: 1,
      opacity: 0.25
    },
    
    // Floating nodes
    nodes: {
      enabled: true,
      count: 60,
      maxSize: 2,
      opacity: 0.5,
      speed: 0.2
    },
    
    // Glow regions
    glows: {
      enabled: true,
      count: 4
    },
    
    // Noise particles (stars)
    noise: {
      enabled: true,
      count: 400,
      opacity: 0.5
    }
  };

  // ═══════════════════════════════════════════════════════════════
  // STATE
  // ═══════════════════════════════════════════════════════════════
  
  let canvas, ctx, config;
  let animationId = null;
  let time = 0;
  let onUpdate = null;
  
  const nodes = [];
  const glows = [];
  
  let seed = 12345;
  function seededRandom() {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  }
  
  function resetSeed() {
    seed = 12345;
  }

  // ═══════════════════════════════════════════════════════════════
  // INITIALIZATION
  // ═══════════════════════════════════════════════════════════════
  
  function generate(options) {
    config = Object.assign({}, DEFAULTS, options);
    
    canvas = document.createElement('canvas');
    canvas.width = config.width;
    canvas.height = config.height;
    ctx = canvas.getContext('2d');
    
    // Clear arrays
    nodes.length = 0;
    glows.length = 0;
    time = 0;
    
    // Initialize nodes
    resetSeed();
    if (config.nodes.enabled) {
      for (let i = 0; i < config.nodes.count; i++) {
        nodes.push({
          x: seededRandom() * config.width,
          y: seededRandom() * config.height,
          vx: (seededRandom() - 0.5) * config.nodes.speed,
          vy: (seededRandom() - 0.5) * config.nodes.speed,
          size: 0.5 + seededRandom() * config.nodes.maxSize,
          color: seededRandom() > 0.5 ? 'cyan' : 'purple',
          pulse: seededRandom() * Math.PI * 2
        });
      }
    }
    
    // Initialize glow regions
    if (config.glows.enabled) {
      for (let i = 0; i < config.glows.count; i++) {
        glows.push({
          x: seededRandom() * config.width,
          y: seededRandom() * config.height,
          radius: 100 + seededRandom() * 200,
          color: seededRandom() > 0.5 ? 'purple' : 'cyan',
          phase: seededRandom() * Math.PI * 2
        });
      }
    }
    
    draw();
    
    console.log('[AcidburnGalaxy] Generated ' + config.width + 'x' + config.height + ' texture');
    
    return canvas;
  }

  // ═══════════════════════════════════════════════════════════════
  // DRAWING
  // ═══════════════════════════════════════════════════════════════
  
  function draw() {
    if (!ctx || !config) return;
    
    const { width, height } = config;
    
    // Clear with dark background
    ctx.fillStyle = config.colors.background;
    ctx.fillRect(0, 0, width, height);
    
    // Base gradient (subtle)
    drawBaseGradient();
    
    // Glow regions
    if (config.glows.enabled) {
      drawGlows();
    }
    
    // Grid
    if (config.grid.enabled) {
      drawGrid();
    }
    
    // Noise
    if (config.noise.enabled) {
      drawNoise();
    }
    
    // Nodes
    if (config.nodes.enabled) {
      drawNodes();
    }
    
    // Callback for texture update
    if (onUpdate) {
      onUpdate(canvas);
    }
  }
  
  function drawBaseGradient() {
    const { width, height, colors } = config;
    
    // Vertical gradient - purple at poles, darker in middle
    const vGrad = ctx.createLinearGradient(0, 0, 0, height);
    vGrad.addColorStop(0, 'rgba(191, 0, 255, 0.08)');
    vGrad.addColorStop(0.3, 'rgba(0, 10, 20, 0.1)');
    vGrad.addColorStop(0.5, 'rgba(0, 255, 255, 0.03)');
    vGrad.addColorStop(0.7, 'rgba(0, 10, 20, 0.1)');
    vGrad.addColorStop(1, 'rgba(191, 0, 255, 0.08)');
    
    ctx.fillStyle = vGrad;
    ctx.fillRect(0, 0, width, height);
  }
  
  function drawGlows() {
    for (const glow of glows) {
      const pulse = Math.sin(time * 0.02 + glow.phase) * 0.3 + 0.7;
      const color = config.colors[glow.color];
      
      const grad = ctx.createRadialGradient(
        glow.x, glow.y, 0,
        glow.x, glow.y, glow.radius * pulse
      );
      
      // Parse hex color to rgba
      const r = parseInt(color.slice(1, 3), 16);
      const g = parseInt(color.slice(3, 5), 16);
      const b = parseInt(color.slice(5, 7), 16);
      
      grad.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.1)`);
      grad.addColorStop(0.5, `rgba(${r}, ${g}, ${b}, 0.03)`);
      grad.addColorStop(1, 'rgba(0, 0, 0, 0)');
      
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, config.width, config.height);
    }
  }
  
  function drawGrid() {
    const { width, height } = config;
    const { latLines, lonLines, opacity, lineWidth } = config.grid;
    
    ctx.globalAlpha = opacity;
    ctx.lineWidth = lineWidth || 1;
    
    // Latitude lines (cyan)
    ctx.strokeStyle = config.colors.cyan;
    for (let i = 0; i <= latLines; i++) {
      const y = (i / latLines) * height;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }
    
    // Longitude lines (purple)
    ctx.strokeStyle = config.colors.purple;
    for (let i = 0; i <= lonLines; i++) {
      const x = (i / lonLines) * width;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();
    }
    
    // Accent points at intersections
    ctx.fillStyle = '#ffffff';
    ctx.globalAlpha = opacity * 0.8;
    for (let i = 0; i <= lonLines; i += 4) {
      for (let j = 0; j <= latLines; j += 4) {
        const x = (i / lonLines) * width;
        const y = (j / latLines) * height;
        ctx.fillRect(x - 1, y - 1, 3, 3);
      }
    }
    
    ctx.globalAlpha = 1;
  }
  
  function drawNoise() {
    resetSeed();
    
    for (let i = 0; i < config.noise.count; i++) {
      const x = seededRandom() * config.width;
      const y = seededRandom() * config.height;
      const size = seededRandom() * 2;
      
      // Vary color
      const colorChoice = seededRandom();
      if (colorChoice > 0.7) {
        ctx.fillStyle = config.colors.cyan;
      } else if (colorChoice > 0.4) {
        ctx.fillStyle = config.colors.purple;
      } else {
        ctx.fillStyle = '#ffffff';
      }
      
      ctx.globalAlpha = config.noise.opacity * (0.3 + seededRandom() * 0.7);
      ctx.fillRect(x, y, size, size);
    }
    
    ctx.globalAlpha = 1;
  }
  
  function drawNodes() {
    for (const node of nodes) {
      // Update position
      if (config.animated) {
        node.x += node.vx;
        node.y += node.vy;
        node.pulse += 0.05;
        
        // Wrap
        if (node.x < 0) node.x += config.width;
        if (node.x > config.width) node.x -= config.width;
        if (node.y < 0) node.y += config.height;
        if (node.y > config.height) node.y -= config.height;
      }
      
      // Pulse effect
      const pulse = Math.sin(node.pulse) * 0.3 + 0.7;
      const size = node.size * pulse;
      
      ctx.globalAlpha = config.nodes.opacity * pulse;
      ctx.fillStyle = config.colors[node.color];
      
      ctx.beginPath();
      ctx.arc(node.x, node.y, size, 0, Math.PI * 2);
      ctx.fill();
      
      // Glow
      ctx.globalAlpha = config.nodes.opacity * 0.3 * pulse;
      ctx.beginPath();
      ctx.arc(node.x, node.y, size * 3, 0, Math.PI * 2);
      ctx.fill();
    }
    
    ctx.globalAlpha = 1;
  }

  // ═══════════════════════════════════════════════════════════════
  // ANIMATION
  // ═══════════════════════════════════════════════════════════════
  
  function start(updateCallback) {
    if (!config) {
      console.warn('[AcidburnGalaxy] Call generate() first');
      return;
    }
    
    config.animated = true;
    onUpdate = updateCallback || null;
    
    const frameInterval = 1000 / config.frameRate;
    let lastFrame = 0;
    
    function animate(timestamp) {
      if (timestamp - lastFrame >= frameInterval) {
        time++;
        draw();
        lastFrame = timestamp;
      }
      animationId = requestAnimationFrame(animate);
    }
    
    animationId = requestAnimationFrame(animate);
    console.log('[AcidburnGalaxy] Animation started');
  }
  
  function stop() {
    if (animationId) {
      cancelAnimationFrame(animationId);
      animationId = null;
      console.log('[AcidburnGalaxy] Animation stopped');
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // PUBLIC API
  // ═══════════════════════════════════════════════════════════════
  
  return {
    generate: generate,
    start: start,
    stop: stop,
    redraw: draw,
    getCanvas: function() { return canvas; },
    getConfig: function() { return config; }
  };
  
})();
