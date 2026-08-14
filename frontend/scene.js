/* ATLAS holographic 3D core — Three.js r128 (global THREE).
   Exposes window.ATLAS3D = { setState, setSpeakAmp, spawnActivity }.
   States: idle | listening | thinking | speaking. */
(function () {
  const PALETTE = {
    idle:      new THREE.Color(0x6ea8fe),
    listening: new THREE.Color(0xff5d7a),
    thinking:  new THREE.Color(0x59e6ff),
    speaking:  new THREE.Color(0x54e6a5),
  };

  let renderer, scene, camera, clock;
  let core, coreMat, shellMat, shell, wire, rings = [], particles, glow;
  let ripples = [];
  const holos = [];
  let state = 'idle';
  let curColor = PALETTE.idle.clone();
  let speakAmp = 0, spin = 1;
  // glTF avatar
  let avatar = null, avatarLoaded = false, avatarLights = null;
  let mouthMorphs = [], blinkMorphs = [], blinkTimer = 2, blinking = 0;
  let avatarBaseY = 0, avatarLookY = 0, defaultCamZ = 8;

  function radialSprite(color) {
    const c = document.createElement('canvas'); c.width = c.height = 256;
    const g = c.getContext('2d');
    const grd = g.createRadialGradient(128, 128, 0, 128, 128, 128);
    grd.addColorStop(0, 'rgba(255,255,255,0.9)');
    grd.addColorStop(0.25, color);
    grd.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = grd; g.fillRect(0, 0, 256, 256);
    return new THREE.CanvasTexture(c);
  }

  const FRESNEL_V = `
    varying vec3 vN; varying vec3 vV;
    void main(){
      vN = normalize(normalMatrix * normal);
      vec4 mv = modelViewMatrix * vec4(position,1.0);
      vV = normalize(-mv.xyz);
      gl_Position = projectionMatrix * mv;
    }`;
  const FRESNEL_F = `
    uniform vec3 uColor; uniform float uPower; uniform float uOpacity;
    varying vec3 vN; varying vec3 vV;
    void main(){
      float f = pow(1.0 - abs(dot(vN, vV)), uPower);
      gl_FragColor = vec4(uColor * f, f * uOpacity);
    }`;
  const CORE_F = `
    uniform vec3 uColor; uniform float uTime; uniform float uAmp;
    varying vec3 vN; varying vec3 vV;
    void main(){
      float facing = pow(max(dot(vN, vV), 0.0), 1.5);
      float pulse = 0.72 + 0.14*sin(uTime*2.2) + uAmp*0.5;
      vec3 col = uColor * (0.35 + 0.65*facing) * pulse;
      gl_FragColor = vec4(col, 1.0);
    }`;

  function init(container) {
    const w = container.clientWidth, h = container.clientHeight;
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true,
      powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2)); // crisp / "4K"
    renderer.setSize(w, h);
    renderer.outputEncoding = THREE.sRGBEncoding;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.15;
    container.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x05060d, 0.055);
    camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 100);
    camera.position.set(0, 0.6, 8);

    scene.add(new THREE.AmbientLight(0x223055, 0.8));
    const p1 = new THREE.PointLight(0x6ea8fe, 1.2, 40); p1.position.set(6, 5, 6);
    const p2 = new THREE.PointLight(0xb78bff, 1.0, 40); p2.position.set(-6, -3, 4);
    scene.add(p1, p2);

    // reflective grid floor
    const grid = new THREE.GridHelper(40, 40, 0x2a3a66, 0x162138);
    grid.position.y = -3.0; grid.material.transparent = true; grid.material.opacity = 0.5;
    scene.add(grid);

    clock = new THREE.Clock();

    // inner core
    coreMat = new THREE.ShaderMaterial({
      uniforms: { uColor: { value: curColor.clone() }, uTime: { value: 0 }, uAmp: { value: 0 } },
      vertexShader: FRESNEL_V, fragmentShader: CORE_F });
    core = new THREE.Mesh(new THREE.SphereGeometry(1.15, 64, 64), coreMat);
    scene.add(core);

    // fresnel shell
    shellMat = new THREE.ShaderMaterial({
      uniforms: { uColor: { value: curColor.clone() }, uPower: { value: 2.6 }, uOpacity: { value: 1.0 } },
      vertexShader: FRESNEL_V, fragmentShader: FRESNEL_F,
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.FrontSide });
    shell = new THREE.Mesh(new THREE.SphereGeometry(1.55, 64, 64), shellMat);
    scene.add(shell);

    // techy wireframe gyroscope
    wire = new THREE.Mesh(new THREE.IcosahedronGeometry(2.0, 1),
      new THREE.MeshBasicMaterial({ color: 0x3f5f9f, wireframe: true, transparent: true, opacity: 0.35 }));
    scene.add(wire);

    // orbiting rings
    for (let i = 0; i < 3; i++) {
      const ring = new THREE.Mesh(new THREE.TorusGeometry(2.5 + i * 0.18, 0.012, 12, 160),
        new THREE.MeshBasicMaterial({ color: curColor.clone(), transparent: true, opacity: 0.8,
          blending: THREE.AdditiveBlending }));
      ring.rotation.x = Math.random() * Math.PI; ring.rotation.y = Math.random() * Math.PI;
      rings.push(ring); scene.add(ring);
    }

    // particle halo
    const N = 1600, pos = new Float32Array(N * 3);
    for (let i = 0; i < N; i++) {
      const r = 2.4 + Math.random() * 3.2, th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
      pos[i*3] = r*Math.sin(ph)*Math.cos(th); pos[i*3+1] = r*Math.cos(ph)*0.6; pos[i*3+2] = r*Math.sin(ph)*Math.sin(th);
    }
    const pg = new THREE.BufferGeometry(); pg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    particles = new THREE.Points(pg, new THREE.PointsMaterial({ color: 0x8fb4ff, size: 0.03,
      transparent: true, opacity: 0.8, blending: THREE.AdditiveBlending, depthWrite: false }));
    scene.add(particles);

    // background bloom glow sprite
    glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: radialSprite('rgba(110,168,254,0.55)'),
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
    glow.scale.set(9, 9, 1); glow.position.z = -1.5; scene.add(glow);

    window.addEventListener('resize', () => onResize(container));
    animate();
  }

  function onResize(container) {
    const w = container.clientWidth, h = container.clientHeight;
    camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h);
  }

  function spawnRipple() {
    const m = new THREE.Mesh(new THREE.TorusGeometry(1.6, 0.02, 8, 120),
      new THREE.MeshBasicMaterial({ color: PALETTE.listening.clone(), transparent: true,
        opacity: 0.9, blending: THREE.AdditiveBlending }));
    m.rotation.x = Math.PI / 2; ripples.push({ m, t: 0 }); scene.add(m);
  }

  function animate() {
    requestAnimationFrame(animate);
    const dt = clock.getDelta(), t = clock.elapsedTime;
    const target = PALETTE[state];
    curColor.lerp(target, Math.min(1, dt * 4));
    spin += ((state === 'thinking' ? 3.2 : state === 'listening' ? 1.6 : 1) - spin) * Math.min(1, dt * 3);

    coreMat.uniforms.uColor.value.copy(curColor);
    coreMat.uniforms.uTime.value = t;
    coreMat.uniforms.uAmp.value += (speakAmp - coreMat.uniforms.uAmp.value) * Math.min(1, dt * 12);
    shellMat.uniforms.uColor.value.copy(curColor);

    const s = 1 + 0.04 * Math.sin(t * 2.2) + (state === 'speaking' ? speakAmp * 0.35 : 0);
    core.scale.setScalar(s);
    wire.rotation.y += dt * 0.25 * spin; wire.rotation.x += dt * 0.12 * spin;
    rings.forEach((r, i) => { r.rotation.z += dt * (0.4 + i * 0.15) * spin;
      r.rotation.x += dt * 0.1 * spin; r.material.color.copy(curColor); });
    particles.rotation.y += dt * 0.06; particles.rotation.x += dt * 0.02;
    glow.material.opacity = 0.4 + 0.15 * Math.sin(t * 1.8) + speakAmp * 0.2;

    if (state === 'listening' && Math.random() < dt * 3) spawnRipple();
    for (let i = ripples.length - 1; i >= 0; i--) {
      const rp = ripples[i]; rp.t += dt; rp.m.scale.setScalar(1 + rp.t * 3);
      rp.m.material.opacity = Math.max(0, 0.9 - rp.t * 0.9);
      if (rp.t > 1) { scene.remove(rp.m); ripples.splice(i, 1); }
    }
    for (let i = holos.length - 1; i >= 0; i--) {
      const ho = holos[i]; ho.t += dt; ho.sp.position.y += dt * 0.6;
      ho.sp.material.opacity = Math.max(0, 1 - ho.t / 3.2);
      if (ho.t > 3.2) { scene.remove(ho.sp); holos.splice(i, 1); }
    }
    if (avatarLoaded && avatar) {
      const amp = coreMat.uniforms.uAmp.value;            // smoothed speak amplitude
      for (const [o, i] of mouthMorphs) o.morphTargetInfluences[i] = Math.min(1, amp * 1.3);
      blinkTimer -= dt;
      if (blinkTimer <= 0) { blinking = 0.12; blinkTimer = 2 + Math.random() * 3.5; }
      let bv = 0; if (blinking > 0) { blinking -= dt; bv = 1; }
      for (const [o, i] of blinkMorphs) o.morphTargetInfluences[i] = bv;
      avatar.rotation.y = Math.sin(t * 0.5) * 0.05;        // idle sway
      avatar.position.y = avatarBaseY + Math.sin(t * 1.4) * 0.02;  // breathing
    }
    camera.position.x = Math.sin(t * 0.15) * (avatarLoaded ? 0.25 : 0.5);
    camera.lookAt(0, avatarLoaded ? avatarLookY : 0, 0);
    renderer.render(scene, camera);
  }

  function roundRect(g, x, y, w, h, r) {
    g.beginPath(); g.moveTo(x + r, y);
    g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
  }

  function ensureAvatarLights() {
    if (avatarLights) return;
    avatarLights = new THREE.Group();
    const hemi = new THREE.HemisphereLight(0xbcd4ff, 0x1a2340, 0.95);
    const key = new THREE.DirectionalLight(0xffffff, 1.15); key.position.set(1, 1.6, 3);
    const fill = new THREE.DirectionalLight(0x88aaff, 0.55); fill.position.set(-2, 0.4, 2);
    avatarLights.add(hemi, key, fill); scene.add(avatarLights);
  }

  window.ATLAS3D = {
    init,
    setState(s) { state = s; },
    setSpeakAmp(v) { speakAmp = Math.max(0, Math.min(1, v)); },

    /* Load a rigged glTF/GLB (e.g. Ready Player Me). Hides the holo-core, frames
       the camera, and wires lip-sync (jaw/mouth morphs) + blink. cb(ok:boolean). */
    loadAvatar(url, cb) {
      if (!url || typeof THREE.GLTFLoader === 'undefined') { if (cb) cb(false); return; }
      new THREE.GLTFLoader().load(url, (gltf) => {
        try {
          if (avatar) scene.remove(avatar);
          avatar = gltf.scene;
          const box = new THREE.Box3().setFromObject(avatar);
          const size = box.getSize(new THREE.Vector3());
          const center = box.getCenter(new THREE.Vector3());
          avatar.position.x -= center.x; avatar.position.y -= center.y; avatar.position.z -= center.z;
          avatarBaseY = avatar.position.y;
          scene.add(avatar);
          const maxDim = Math.max(size.x, size.y);
          const dist = (maxDim / 2) / Math.tan((camera.fov * Math.PI / 180) / 2) * 1.3;
          camera.position.set(0, size.y * 0.06, dist); avatarLookY = 0;
          mouthMorphs = []; blinkMorphs = [];
          avatar.traverse(o => {
            if (o.isMesh && o.morphTargetDictionary) {
              const d = o.morphTargetDictionary;
              ['jawOpen', 'mouthOpen', 'viseme_aa', 'mouthFunnel'].forEach(n => {
                if (n in d) mouthMorphs.push([o, d[n]]); });
              ['eyeBlinkLeft', 'eyeBlinkRight', 'eyesClosed', 'blink'].forEach(n => {
                if (n in d) blinkMorphs.push([o, d[n]]); });
            }
          });
          [core, shell, wire].forEach(m => { if (m) m.visible = false; });
          rings.forEach(r => r.visible = false);
          ensureAvatarLights();
          avatarLoaded = true;
          if (cb) cb(true);
        } catch (e) { console.warn('avatar setup failed', e); if (cb) cb(false); }
      }, undefined, (err) => { console.warn('avatar load failed', err); if (cb) cb(false); });
    },

    resetAvatar() {
      if (avatar) { scene.remove(avatar); avatar = null; }
      avatarLoaded = false; mouthMorphs = []; blinkMorphs = [];
      [core, shell, wire].forEach(m => { if (m) m.visible = true; });
      rings.forEach(r => r.visible = true);
      camera.position.set(0, 0.6, defaultCamZ); avatarLookY = 0;
    },
    spawnActivity(icon, label) {
      const c = document.createElement('canvas'); c.width = 360; c.height = 150;
      const g = c.getContext('2d');
      g.fillStyle = 'rgba(20,28,52,0.85)'; roundRect(g, 6, 6, 348, 138, 22); g.fill();
      g.strokeStyle = 'rgba(120,180,255,0.6)'; g.lineWidth = 2; g.stroke();
      g.font = '70px serif'; g.textBaseline = 'middle'; g.fillText(icon || '⚙️', 28, 78);
      g.fillStyle = '#eaf0fb'; g.font = 'bold 30px Segoe UI, sans-serif';
      g.fillText((label || '').slice(0, 20), 120, 78);
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c),
        transparent: true, depthWrite: false }));
      sp.scale.set(2.6, 1.08, 1);
      const ang = Math.random() * Math.PI * 2;
      sp.position.set(Math.cos(ang) * 2.6, 0.5, Math.sin(ang) * 2.6 + 0.5);
      holos.push({ sp, t: 0 }); scene.add(sp);
    },
  };
})();
