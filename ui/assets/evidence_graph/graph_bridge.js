(function () {
  const container = document.getElementById('graph');
  const tooltip = document.getElementById('tooltip');
  const subject = document.getElementById('subject');
  const meta = document.getElementById('meta');

  const textureCache = new Map();
  const pulseMaterials = [];
  let graph = null;
  let graphData = { nodes: [], links: [], meta: {} };
  let focusLayers = new Set();

  function init() {
    if (typeof ForceGraph3D === 'undefined' || typeof THREE === 'undefined') {
      document.body.classList.add('engine-missing');
      return;
    }

    graph = ForceGraph3D({ controlType: 'orbit', rendererConfig: { antialias: true, alpha: true } })(container)
      .backgroundColor('rgba(0,0,0,0)')
      .showNavInfo(false)
      .enableNodeDrag(false)
      .nodeLabel(() => '')
      .nodeVal((node) => node.val || 2)
      .nodeThreeObject(createNodeObject)
      .linkColor(linkColor)
      .linkOpacity(1)
      .linkWidth(linkWidth)
      .linkCurvature((link) => link.visual_only ? 0 : Number(link.curvature || (link.is_primary ? 0.048 : 0.018)))
      .linkDirectionalParticles(particleCount)
      .linkDirectionalParticleWidth((link) => isLinkActive(link) ? 1.6 : 0.8)
      .linkDirectionalParticleSpeed((link) => link.is_primary ? 0.004 : 0.0025)
      .linkDirectionalParticleColor((link) => withAlpha(link.color || '#2a9df4', isLinkActive(link) ? 0.95 : 0.28))
      .cooldownTicks(1)
      .d3VelocityDecay(0.82)
      .onNodeHover(onNodeHover)
      .onNodeClick((node) => {
        if (!node || node.visual_only || node.clickable === false) return;
        focusNode(node, 270);
      });

    const controls = graph.controls();
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.32;
    controls.enableDamping = true;
    controls.dampingFactor = 0.055;
    controls.minDistance = 130;
    controls.maxDistance = 820;

    configureRenderer();
    configureScene();
    window.setInterval(updatePulse, 80);

    window.addEventListener('resize', resize);
    resize();
  }

  function setGraph(data) {
    graphData = normalizeGraph(data || {});
    subject.textContent = `${graphData.meta.subject_code || ''} ${graphData.meta.subject_name || 'EvidenceGraph'}`.trim();
    meta.textContent = `证据 ${graphData.meta.node_count || 0}/${graphData.meta.link_count || 0} · 渲染 ${graphData.nodes.length}/${graphData.links.length}`;

    if (!graph) init();
    if (!graph) return;

    graph.graphData(graphData);
    refreshAccessors();
    setTimeout(frameGlobe, 180);
  }

  function focusLayersApi(layers) {
    focusLayers = new Set((layers || []).filter(Boolean));
    if (!graph) return;
    refreshAccessors();
    graph.d3ReheatSimulation();

    const activeNodes = graphData.nodes.filter((node) =>
      !node.visual_only && (node.type === 'stock' || focusLayers.has(node.layer))
    );
    if (!activeNodes.length) {
      frameGlobe();
      return;
    }
    focusCluster(activeNodes);
  }

  function refreshAccessors() {
    graph.nodeThreeObject(createNodeObject);
    graph.linkColor(linkColor);
    graph.linkWidth(linkWidth);
    graph.linkDirectionalParticles(particleCount);
  }

  function frameGlobe() {
    if (!graph || !graphData.nodes.length) return;
    const dist = Number(graphData.meta.camera_distance || 520);
    const target = {
      x: 0,
      y: Number(graphData.meta.camera_target_y || 12),
      z: Number(graphData.meta.camera_target_z || 18),
    };
    graph.cameraPosition({ x: 0, y: target.y - 32, z: dist }, target, 850);
  }

  function focusCluster(nodes) {
    const realNodes = nodes.filter((node) => !node.visual_only);
    if (!realNodes.length) return;
    const center = centroid(realNodes);
    const len = Math.hypot(center.x, center.y, center.z) || 1;
    const distance = 245;
    graph.cameraPosition(
      {
        x: center.x + (center.x / len) * distance,
        y: center.y + (center.y / len) * distance - 18,
        z: center.z + (center.z / len) * distance + 60,
      },
      center,
      760
    );
  }

  function focusNode(node, distance) {
    if (!graph || !node) return;
    const target = {
      x: node.x || 0,
      y: node.y || 0,
      z: node.z || 0,
    };
    const len = Math.hypot(target.x, target.y, target.z) || 1;
    const dist = distance || 270;
    graph.cameraPosition(
      {
        x: target.x + (target.x / len) * dist,
        y: target.y + (target.y / len) * dist - 12,
        z: target.z + (target.z / len) * dist + 48,
      },
      target,
      760
    );
    showTooltip(node, window.innerWidth * 0.58, window.innerHeight * 0.22);
  }

  function createNodeObject(node) {
    if (node.visual_only) return createParticleObject(node);

    const role = node.visual_role || 'evidence';
    const color = nodeColor(node);
    const active = isNodeActive(node);
    const value = Math.max(0.8, Math.min(14, Number(node.val || 3)));
    const group = new THREE.Group();

    if (role === 'stock') {
      group.add(makeSprite('#4fc3ff', value * 5.4, active ? 0.46 : 0.20, 'glow'));
      group.add(makeSprite('#f7fbff', value * 2.4, active ? 0.18 : 0.08, 'glow'));
      if (isPerformanceMode()) group.add(makePulsingSprite('#4fc3ff', value * 3.25, 0.18, 0.16));
      group.add(makeMesh(value * 0.48, '#f7fbff', 1.0));
      group.add(makeRing('#4fc3ff', value * 2.25, false, active ? 0.75 : 0.34));
      return group;
    }

    if (role === 'forecast') {
      group.add(makeSprite('#bffcff', value * 3.6, active ? (isPerformanceMode() ? 0.34 : 0.18) : 0.06, 'dashed-ring'));
      group.add(makeMesh(value * 0.32, '#bffcff', active ? 0.86 : 0.24));
      return group;
    }

    if (role === 'missing') {
      group.add(makeSprite('#8a3346', value * 3.4, active ? 0.20 : 0.05, 'glow'));
      group.add(makeRing('#6f7f99', value * 2.3, true, active ? 0.58 : 0.14));
      group.add(makeMesh(value * 0.33, '#6f7f99', active ? 0.82 : 0.22));
      return group;
    }

    if (role === 'risk' || role === 'strong') {
      group.add(makeSprite(color, value * 3.1, active ? 0.25 : 0.06, 'glow'));
      group.add(makeRing(color, value * 1.95, false, active ? 0.48 : 0.10));
      group.add(makeMesh(value * 0.36, color, active ? 0.92 : 0.24));
      return group;
    }

    group.add(makeSprite(color, value * 2.2, active ? 0.16 : 0.04, 'glow'));
    group.add(makeMesh(value * 0.30, color, active ? 0.82 : 0.20));
    return group;
  }

  function createParticleObject(node) {
    const active = isNodeActive(node);
    const size = Math.max(0.8, Math.min(1.5, Number(node.val || 1))) * 2.35;
    const depth = Number(node.depth_alpha || node.opacity || 0.18);
    const modeScale = isPerformanceMode() ? 1 : 0.54;
    return makeSprite(node.color || '#2a9df4', size * modeScale, active ? depth * modeScale : Math.min(0.08, depth * 0.35), 'dot');
  }

  function makeMesh(radius, color, opacity) {
    const geometry = new THREE.SphereGeometry(radius, 10, 8);
    const material = THREE.MeshStandardMaterial
      ? new THREE.MeshStandardMaterial({
        color,
        emissive: color,
        emissiveIntensity: isPerformanceMode() ? 0.32 : 0.18,
        roughness: 0.42,
        metalness: 0.18,
        transparent: opacity < 1,
        opacity,
        depthWrite: opacity >= 0.85,
      })
      : new THREE.MeshBasicMaterial({
        color,
        transparent: opacity < 1,
        opacity,
        depthWrite: opacity >= 0.85,
      });
    return new THREE.Mesh(geometry, material);
  }

  function makeSprite(color, size, opacity, kind) {
    const material = new THREE.SpriteMaterial({
      map: texture(kind, color),
      color: '#ffffff',
      transparent: true,
      opacity,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(size, size, 1);
    return sprite;
  }

  function makePulsingSprite(color, size, baseOpacity, amplitude) {
    const sprite = makeSprite(color, size, baseOpacity, 'ring');
    sprite.material.userData.baseOpacity = baseOpacity;
    sprite.material.userData.amplitude = amplitude;
    pulseMaterials.push(sprite.material);
    return sprite;
  }

  function makeRing(color, size, dashed, opacity) {
    return makeSprite(color, size, opacity, dashed ? 'dashed-ring' : 'ring');
  }

  function makeSceneGlow() {
    const sprite = makeSprite('#4fc3ff', 230, 0.115, 'glow');
    sprite.position.set(0, 10, 24);
    return sprite;
  }

  function configureRenderer() {
    const renderer = graph && graph.renderer && graph.renderer();
    if (!renderer) return;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, isPerformanceMode() ? 1.75 : 1.15));
    if (THREE.ACESFilmicToneMapping) renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = isPerformanceMode() ? 1.18 : 0.92;
    if ('outputColorSpace' in renderer && THREE.SRGBColorSpace) renderer.outputColorSpace = THREE.SRGBColorSpace;
  }

  function configureScene() {
    const scene = graph.scene();
    scene.fog = new THREE.FogExp2('#07101f', isPerformanceMode() ? 0.00105 : 0.00165);
    scene.add(new THREE.AmbientLight('#6f9fd8', isPerformanceMode() ? 0.55 : 0.28));
    const key = new THREE.PointLight('#4fc3ff', isPerformanceMode() ? 1.55 : 0.72, 620);
    key.position.set(-70, -90, 240);
    scene.add(key);
    const rim = new THREE.PointLight('#31f28a', isPerformanceMode() ? 0.72 : 0.24, 460);
    rim.position.set(210, 120, -120);
    scene.add(rim);
    if (isPerformanceMode()) scene.add(makeSceneGlow());
  }

  function updatePulse() {
    if (!pulseMaterials.length) return;
    const phase = (Date.now() % 2600) / 2600;
    const wave = 0.5 + 0.5 * Math.sin(phase * Math.PI * 2);
    pulseMaterials.forEach((material) => {
      const base = Number(material.userData.baseOpacity || 0.16);
      const amp = Number(material.userData.amplitude || 0.12);
      material.opacity = base + wave * amp;
    });
  }

  function texture(kind, color) {
    const key = `${kind}:${color}`;
    if (textureCache.has(key)) return textureCache.get(key);

    const canvas = document.createElement('canvas');
    canvas.width = 128;
    canvas.height = 128;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, 128, 128);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineCap = 'round';

    if (kind === 'dot') {
      const grad = ctx.createRadialGradient(64, 64, 0, 64, 64, 54);
      grad.addColorStop(0, withAlpha(color, 0.95));
      grad.addColorStop(0.26, withAlpha(color, 0.42));
      grad.addColorStop(1, withAlpha(color, 0));
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, 128, 128);
    } else if (kind === 'ring' || kind === 'dashed-ring') {
      ctx.lineWidth = kind === 'dashed-ring' ? 5 : 4;
      if (kind === 'dashed-ring') ctx.setLineDash([13, 9]);
      ctx.beginPath();
      ctx.arc(64, 64, 42, 0, Math.PI * 2);
      ctx.stroke();
      const grad = ctx.createRadialGradient(64, 64, 26, 64, 64, 58);
      grad.addColorStop(0, withAlpha(color, 0));
      grad.addColorStop(0.66, withAlpha(color, 0.18));
      grad.addColorStop(1, withAlpha(color, 0));
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, 128, 128);
    } else {
      const grad = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
      grad.addColorStop(0, withAlpha(color, 0.55));
      grad.addColorStop(0.22, withAlpha(color, 0.22));
      grad.addColorStop(1, withAlpha(color, 0));
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, 128, 128);
    }

    const tex = new THREE.CanvasTexture(canvas);
    textureCache.set(key, tex);
    return tex;
  }

  function linkWidth(link) {
    const active = isLinkActive(link);
    if (link.visual_only) {
      return active ? Number(link.width || 0.24) : 0.05;
    }
    const base = Number(link.width || (link.is_primary ? 1.1 : 0.65));
    if (!focusLayers.size) return link.is_primary ? Math.max(0.9, base) : Math.max(0.42, base * 0.82);
    return active ? Math.min(3.0, Math.max(1.8, base * 1.75)) : 0.16;
  }

  function linkColor(link) {
    const active = isLinkActive(link);
    const color = link.color || '#2a9df4';
    if (link.visual_only) {
      const depth = Number(link.depth_alpha || link.opacity || 0.14);
      return withAlpha(color, active ? depth : 0.035);
    }
    if (!focusLayers.size) return withAlpha(color, link.is_primary ? 0.38 : 0.24);
    return withAlpha(color, active ? 0.82 : 0.12);
  }

  function particleCount(link) {
    if (link.visual_only) return 0;
    if (isEfficiencyMode()) return 0;
    if (focusLayers.size) {
      if (!isLinkActive(link)) return 0;
      return link.is_primary ? 6 : 3;
    }
    return link.is_primary && isPerformanceMode() ? 2 : 0;
  }

  function onNodeHover(node) {
    if (!node || node.visual_only || node.clickable === false) {
      tooltip.style.display = 'none';
      return;
    }
    showTooltip(node);
  }

  function showTooltip(node, fixedX, fixedY) {
    const name = escapeHtml(node.name || node.label || node.id || '');
    const summary = escapeHtml(node.summary || '');
    const layer = escapeHtml(node.layer || '');
    const role = escapeHtml(node.forecast_role || node.visual_role || 'fact');
    tooltip.innerHTML = `<strong>${name}</strong><div>${layer} · ${role}</div><div>${summary}</div>`;
    tooltip.style.display = 'block';
    const x = fixedX || window.event?.clientX || 24;
    const y = fixedY || window.event?.clientY || 24;
    tooltip.style.left = `${Math.min(window.innerWidth - 348, x + 14)}px`;
    tooltip.style.top = `${Math.min(window.innerHeight - 146, y + 14)}px`;
  }

  function isNodeActive(node) {
    return !focusLayers.size || node.type === 'stock' || focusLayers.has(node.layer);
  }

  function isLinkActive(link) {
    return !focusLayers.size || focusLayers.has(link.layer);
  }

  function nodeColor(node) {
    if (node.type === 'stock') return '#f7fbff';
    if (!isNodeActive(node)) return '#354965';
    return node.color || '#93a4c8';
  }

  function isPerformanceMode() {
    return !graphData.meta || graphData.meta.performance_mode !== '效率';
  }

  function isEfficiencyMode() {
    return graphData.meta && graphData.meta.performance_mode === '效率';
  }

  function centroid(nodes) {
    const total = nodes.length || 1;
    const sum = nodes.reduce((acc, node) => {
      acc.x += Number(node.x || 0);
      acc.y += Number(node.y || 0);
      acc.z += Number(node.z || 0);
      return acc;
    }, { x: 0, y: 0, z: 0 });
    return { x: sum.x / total, y: sum.y / total, z: sum.z / total };
  }

  function normalizeGraph(data) {
    return {
      nodes: Array.isArray(data.nodes) ? data.nodes : [],
      links: Array.isArray(data.links) ? data.links : [],
      meta: data.meta || {},
    };
  }

  function resize() {
    if (!graph) return;
    graph.width(window.innerWidth);
    graph.height(window.innerHeight);
  }

  function withAlpha(hex, alpha) {
    if (!hex || !hex.startsWith('#')) return hex || `rgba(147,164,200,${alpha})`;
    const normalized = hex.length === 4
      ? `#${hex[1]}${hex[1]}${hex[2]}${hex[2]}${hex[3]}${hex[3]}`
      : hex;
    const int = parseInt(normalized.slice(1), 16);
    const r = (int >> 16) & 255;
    const g = (int >> 8) & 255;
    const b = int & 255;
    return `rgba(${r},${g},${b},${alpha})`;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  window.AldebaranGraph = {
    setGraph,
    focusLayers: focusLayersApi,
  };

  init();
})();
