/**
 * Loom — Schwarzschild Black Hole Raytracer
 * Based on oseiskar/black-hole, acidburn fork by pyokosmeme
 * Pre-compiled GLSL (Mustache resolved), self-contained with Three.js r128
 *
 * Features enabled: accretion disk, aberration, beaming, doppler shift,
 * light travel time, lorentz contraction, gravitational time dilation,
 * observer orbital motion. Planet disabled.
 */

(function () {
    'use strict';

    const container = document.getElementById('blackhole-container');
    if (!container) return;

    if (typeof THREE === 'undefined') {
        console.warn('[Blackhole] Three.js not loaded, using CSS fallback');
        container.style.background = 'radial-gradient(ellipse at 50% 50%, #0a0015 0%, #05000a 40%, #000 100%)';
        return;
    }

    // ── Three.js monkey patch: Matrix4.linearPart() → Matrix3 ──
    THREE.Matrix4.prototype.linearPart = function () {
        var e = this.elements;
        var m = new THREE.Matrix3();
        m.set(e[0], e[4], e[8], e[1], e[5], e[9], e[2], e[6], e[10]);
        return m;
    };

    // ── Vertex shader ──
    const vertexShader = `void main() { gl_Position = vec4(position, 1.0); }`;

    // ── Pre-compiled fragment shader ──
    // Mustache resolved with: n_steps=100, accretion_disk=true, planet=false,
    // aberration=true, beaming=true, doppler_shift=true, light_travel_time=true,
    // lorentz_contraction=true, gravitational_time_dilation=true, observerMotion=true
    const fragmentShader = `
precision highp float;

#define M_PI 3.141592653589793238462643383279
#define R_SQRT_2 0.7071067811865475
#define DEG_TO_RAD (M_PI/180.0)
#define SQ(x) ((x)*(x))
#define ROT_Y(a) mat3(0, cos(a), sin(a), 1, 0, 0, 0, sin(a), -cos(a))

const float BLACK_BODY_TEXTURE_COORD = 1.0;
const float SINGLE_WAVELENGTH_TEXTURE_COORD = 0.5;
const float TEMPERATURE_LOOKUP_RATIO_TEXTURE_COORD = 0.0;

const float SPECTRUM_TEX_TEMPERATURE_RANGE = 65504.0;
const float SPECTRUM_TEX_WAVELENGTH_RANGE = 2048.0;
const float SPECTRUM_TEX_RATIO_RANGE = 6.48053329012;

#define BLACK_BODY_COLOR(t) texture2D(spectrum_texture, vec2((t) / SPECTRUM_TEX_TEMPERATURE_RANGE, BLACK_BODY_TEXTURE_COORD))
#define SINGLE_WAVELENGTH_COLOR(lambda) texture2D(spectrum_texture, vec2((lambda) / SPECTRUM_TEX_WAVELENGTH_RANGE, SINGLE_WAVELENGTH_TEXTURE_COORD))
#define TEMPERATURE_LOOKUP(ratio) (texture2D(spectrum_texture, vec2((ratio) / SPECTRUM_TEX_RATIO_RANGE, TEMPERATURE_LOOKUP_RATIO_TEXTURE_COORD)).r * SPECTRUM_TEX_TEMPERATURE_RANGE)

uniform vec2 resolution;
uniform float time;
uniform vec3 cam_pos;
uniform vec3 cam_x;
uniform vec3 cam_y;
uniform vec3 cam_z;
uniform vec3 cam_vel;

uniform sampler2D galaxy_texture, star_texture,
    accretion_disk_texture, spectrum_texture;

const int NSTEPS = 100;
const float MAX_REVOLUTIONS = 2.0;

const float ACCRETION_MIN_R = 1.5;
const float ACCRETION_WIDTH = 5.0;
const float ACCRETION_BRIGHTNESS = 0.9;
const float ACCRETION_TEMPERATURE = 3900.0;

const float STAR_MIN_TEMPERATURE = 4000.0;
const float STAR_MAX_TEMPERATURE = 15000.0;

const float STAR_BRIGHTNESS = 1.0;
const float GALAXY_BRIGHTNESS = 0.4;

mat3 BG_COORDS = ROT_Y(45.0 * DEG_TO_RAD);

const float FOV_ANGLE_DEG = 90.0;
float FOV_MULT = 1.0 / tan(DEG_TO_RAD * FOV_ANGLE_DEG * 0.5);

vec2 sphere_map(vec3 p) {
    return vec2(atan(p.x, p.y) / M_PI * 0.5 + 0.5, asin(p.z) / M_PI + 0.5);
}

float smooth_step(float x, float threshold) {
    const float STEEPNESS = 1.0;
    return 1.0 / (1.0 + exp(-(x - threshold) * STEEPNESS));
}

vec3 lorentz_velocity_transformation(vec3 moving_v, vec3 frame_v) {
    float v = length(frame_v);
    if (v > 0.0) {
        vec3 v_axis = -frame_v / v;
        float gamma = 1.0 / sqrt(1.0 - v * v);
        float moving_par = dot(moving_v, v_axis);
        vec3 moving_perp = moving_v - v_axis * moving_par;
        float denom = 1.0 + v * moving_par;
        return (v_axis * (moving_par + v) + moving_perp / gamma) / denom;
    }
    return moving_v;
}

vec4 galaxy_color(vec2 tex_coord, float doppler_factor) {
    vec4 color = texture2D(galaxy_texture, tex_coord);

    vec4 ret = vec4(0.0, 0.0, 0.0, 0.0);
    float red = max(0.0, color.r - color.g);

    const float H_ALPHA_RATIO = 0.1;
    const float TEMPERATURE_BIAS = 0.95;

    color.r -= red * H_ALPHA_RATIO;

    float i1 = max(color.r, max(color.g, color.b));
    float ratio = (color.g + color.b) / color.r;

    if (i1 > 0.0 && color.r > 0.0) {
        float temperature = TEMPERATURE_LOOKUP(ratio) * TEMPERATURE_BIAS;
        color = BLACK_BODY_COLOR(temperature);

        float i0 = max(color.r, max(color.g, color.b));
        if (i0 > 0.0) {
            temperature /= doppler_factor;
            ret = BLACK_BODY_COLOR(temperature) * max(i1 / i0, 0.0);
        }
    }

    ret += SINGLE_WAVELENGTH_COLOR(656.28 * doppler_factor) * red / 0.214 * H_ALPHA_RATIO;

    return ret;
}

void main() {
    vec2 p = -1.0 + 2.0 * gl_FragCoord.xy / resolution.xy;
    p.y *= resolution.y / resolution.x;

    vec3 pos = cam_pos;
    vec3 ray = normalize(p.x * cam_x + p.y * cam_y + FOV_MULT * cam_z);

    // aberration
    ray = lorentz_velocity_transformation(ray, cam_vel);

    float ray_intensity = 1.0;
    float ray_doppler_factor = 1.0;

    float gamma = 1.0 / sqrt(1.0 - dot(cam_vel, cam_vel));
    ray_doppler_factor = gamma * (1.0 + dot(ray, -cam_vel));

    // beaming
    ray_intensity /= ray_doppler_factor * ray_doppler_factor * ray_doppler_factor;

    float step = 0.01;
    vec4 color = vec4(0.0, 0.0, 0.0, 1.0);

    float u = 1.0 / length(pos), old_u;
    float u0 = u;

    vec3 normal_vec = normalize(pos);
    vec3 tangent_vec = normalize(cross(cross(normal_vec, ray), normal_vec));

    float du = -dot(ray, normal_vec) / dot(ray, tangent_vec) * u;
    float du0 = du;

    float phi = 0.0;
    float t = time;
    float dt = 1.0;

    vec3 old_pos;

    for (int j = 0; j < NSTEPS; j++) {
        step = MAX_REVOLUTIONS * 2.0 * M_PI / float(NSTEPS);

        float max_rel_u_change = (1.0 - log(u)) * 10.0 / float(NSTEPS);
        if ((du > 0.0 || (du0 < 0.0 && u0 / u < 5.0)) && abs(du) > abs(max_rel_u_change * u) / step)
            step = max_rel_u_change * u / abs(du);

        old_u = u;

        // light_travel_time + gravitational_time_dilation
        dt = sqrt(du * du + u * u * (1.0 - u)) / (u * u * (1.0 - u)) * step;

        // Leapfrog integrator
        u += du * step;
        float ddu = -u * (1.0 - 1.5 * u * u);
        du += ddu * step;

        if (u < 0.0) break;

        phi += step;

        old_pos = pos;
        pos = (cos(phi) * normal_vec + sin(phi) * tangent_vec) / u;

        ray = pos - old_pos;
        float solid_isec_t = 2.0;
        float ray_l = length(ray);

        // light_travel_time + gravitational_time_dilation
        float m = smooth_step(1.0 / u, 8.0);
        dt = m * ray_l + (1.0 - m) * dt;

        // accretion disk intersection
        if (old_pos.z * pos.z < 0.0) {
            float acc_isec_t = -old_pos.z / ray.z;
            if (acc_isec_t < solid_isec_t) {
                vec3 isec = old_pos + ray * acc_isec_t;
                float r = length(isec);

                if (r > ACCRETION_MIN_R) {
                    vec2 tex_coord = vec2(
                        (r - ACCRETION_MIN_R) / ACCRETION_WIDTH,
                        fract(atan(isec.x, isec.y) / M_PI * 0.5 + 0.5 + time * 0.01)
                    );

                    float accretion_intensity = ACCRETION_BRIGHTNESS;
                    float temperature = ACCRETION_TEMPERATURE;

                    vec3 accretion_v = vec3(-isec.y, isec.x, 0.0) / sqrt(2.0 * (r - 1.0)) / (r * r);
                    gamma = 1.0 / sqrt(1.0 - dot(accretion_v, accretion_v));
                    float doppler_factor = gamma * (1.0 + dot(ray / ray_l, accretion_v));

                    // beaming
                    accretion_intensity /= doppler_factor * doppler_factor * doppler_factor;

                    // doppler shift
                    temperature /= ray_doppler_factor * doppler_factor;

                    color += texture2D(accretion_disk_texture, tex_coord)
                        * accretion_intensity
                        * BLACK_BODY_COLOR(temperature);
                }
            }
        }

        // light travel time
        t -= dt;

        if (solid_isec_t <= 1.0) u = 2.0;
        if (u > 1.0) break;
    }

    // background (outside event horizon)
    if (u < 1.0) {
        ray = normalize(pos - old_pos);
        vec2 tex_coord = sphere_map(ray * BG_COORDS);

        vec4 star_color = texture2D(star_texture, tex_coord);
        if (star_color.r > 0.0) {
            float t_coord = (STAR_MIN_TEMPERATURE +
                (STAR_MAX_TEMPERATURE - STAR_MIN_TEMPERATURE) * star_color.g)
                / ray_doppler_factor;
            color += BLACK_BODY_COLOR(t_coord) * star_color.r * STAR_BRIGHTNESS;
        }

        color += galaxy_color(tex_coord, ray_doppler_factor) * GALAXY_BRIGHTNESS;
    }

    gl_FragColor = color * ray_intensity;
}
`;

    // ── Observer class ──

    function Observer() {
        this.position = new THREE.Vector3(10, 0, 0);
        this.velocity = new THREE.Vector3(0, 1, 0);
        this.orientation = new THREE.Matrix3();
        this.time = 0.0;
    }

    const PARAMS = {
        time_scale: 0.5,
        observer_distance: 8.0,
        observer_orbital_inclination: -15,
        gravitational_time_dilation: true,
    };

    function degToRad(a) {
        return (Math.PI * a) / 180.0;
    }

    Observer.prototype.orbitalFrame = function () {
        var orbital_y = new THREE.Vector3()
            .subVectors(
                this.velocity.clone().normalize().multiplyScalar(4.0),
                this.position
            )
            .normalize();
        var orbital_z = new THREE.Vector3()
            .crossVectors(this.position, orbital_y)
            .normalize();
        var orbital_x = new THREE.Vector3().crossVectors(orbital_y, orbital_z);
        return new THREE.Matrix4()
            .makeBasis(orbital_x, orbital_y, orbital_z)
            .linearPart();
    };

    Observer.prototype.move = function (dt) {
        dt *= PARAMS.time_scale;

        var r = PARAMS.observer_distance;
        var v = 1.0 / Math.sqrt(2.0 * (r - 1.0));
        var ang_vel = v / r;
        var angle = this.time * ang_vel;
        var s = Math.sin(angle), c = Math.cos(angle);

        this.position.set(c * r, s * r, 0);
        this.velocity.set(-s * v, c * v, 0);

        var alpha = degToRad(PARAMS.observer_orbital_inclination);
        var orbit_coords = new THREE.Matrix4().makeRotationY(alpha);
        this.position.applyMatrix4(orbit_coords);
        this.velocity.applyMatrix4(orbit_coords);

        if (PARAMS.gravitational_time_dilation) {
            dt = Math.sqrt((dt * dt * (1.0 - v * v)) / (1 - 1.0 / r));
        }
        this.time += dt;
    };

    // ── Texture loading ──

    var textures = {};
    var galaxyTexture = null;
    var loaded = 0;
    var totalTextures = 4;

    function onTextureLoaded() {
        loaded++;
        if (loaded >= totalTextures) {
            initRenderer();
        }
    }

    function loadTexture(key, path, filter) {
        var loader = new THREE.TextureLoader();
        loader.load(
            path,
            function (tex) {
                tex.magFilter = filter;
                tex.minFilter = filter;
                textures[key] = tex;
                onTextureLoaded();
            },
            undefined,
            function () {
                console.warn('[Blackhole] Failed to load texture:', path);
                var canvas = document.createElement('canvas');
                canvas.width = canvas.height = 64;
                var ctx = canvas.getContext('2d');
                ctx.fillStyle = '#000';
                ctx.fillRect(0, 0, 64, 64);
                textures[key] = new THREE.CanvasTexture(canvas);
                onTextureLoaded();
            }
        );
    }

    function loadAcidburnGalaxy() {
        if (typeof AcidburnGalaxy !== 'undefined') {
            console.log('[Blackhole] Generating acidburn procedural galaxy...');
            var galaxyCanvas = AcidburnGalaxy.generate({
                width: 2048,
                height: 1024,
                animated: true
            });

            galaxyTexture = new THREE.CanvasTexture(galaxyCanvas);
            galaxyTexture.magFilter = THREE.LinearFilter;
            galaxyTexture.minFilter = THREE.LinearFilter;
            galaxyTexture.wrapS = THREE.RepeatWrapping;
            galaxyTexture.wrapT = THREE.RepeatWrapping;
            textures.galaxy = galaxyTexture;

            AcidburnGalaxy.start(function () {
                if (galaxyTexture) {
                    galaxyTexture.needsUpdate = true;
                }
            });

            console.log('[Blackhole] Acidburn galaxy texture ready (animated)');
            onTextureLoaded();
        } else {
            console.log('[Blackhole] AcidburnGalaxy not available, falling back to milkyway.jpg');
            loadTexture('galaxy', '/static/img/milkyway.jpg', THREE.NearestFilter);
        }
    }

    // ── Renderer ──

    var renderer, scene, camera, observer, updateUniforms;

    function initRenderer() {
        observer = new Observer();

        renderer = new THREE.WebGLRenderer({ antialias: false });
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        renderer.setSize(window.innerWidth, window.innerHeight);
        container.appendChild(renderer.domElement);

        scene = new THREE.Scene();
        camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 1, 80000);

        // Initialize camera orientation
        var pitchAngle = 3.0, yawAngle = 0.0;
        camera.matrixWorldInverse.makeRotationX(degToRad(-pitchAngle));
        camera.matrixWorldInverse.multiply(
            new THREE.Matrix4().makeRotationY(degToRad(-yawAngle))
        );
        var cm = camera.matrixWorldInverse.elements;
        camera.position.set(cm[2], cm[6], cm[10]);

        var uniforms = {
            time: { value: 0.0 },
            resolution: { value: new THREE.Vector2() },
            cam_pos: { value: new THREE.Vector3() },
            cam_x: { value: new THREE.Vector3() },
            cam_y: { value: new THREE.Vector3() },
            cam_z: { value: new THREE.Vector3() },
            cam_vel: { value: new THREE.Vector3() },
            star_texture: { value: textures.stars },
            accretion_disk_texture: { value: textures.accretion_disk },
            galaxy_texture: { value: textures.galaxy },
            spectrum_texture: { value: textures.spectra },
        };

        updateUniforms = function () {
            uniforms.resolution.value.x = renderer.domElement.width;
            uniforms.resolution.value.y = renderer.domElement.height;
            uniforms.time.value = observer.time;
            uniforms.cam_pos.value.copy(observer.position);

            var e = observer.orientation.elements;
            uniforms.cam_x.value.set(e[0], e[1], e[2]);
            uniforms.cam_y.value.set(e[3], e[4], e[5]);
            uniforms.cam_z.value.set(e[6], e[7], e[8]);
            uniforms.cam_vel.value.copy(observer.velocity);
        };

        var material = new THREE.ShaderMaterial({
            uniforms: uniforms,
            vertexShader: vertexShader,
            fragmentShader: fragmentShader,
            depthWrite: false,
            depthTest: false,
        });

        var mesh = new THREE.Mesh(new THREE.PlaneBufferGeometry(2, 2), material);
        scene.add(mesh);

        updateCamera();
        onWindowResize();
        window.addEventListener('resize', onWindowResize, false);

        animate();
        console.log('[Blackhole] Schwarzschild raytracer initialized');
    }

    function onWindowResize() {
        if (renderer) {
            renderer.setSize(window.innerWidth, window.innerHeight);
            if (updateUniforms) updateUniforms();
        }
    }

    function updateCamera() {
        var m = camera.matrixWorldInverse.elements;
        var camera_matrix = new THREE.Matrix3();
        camera_matrix.set(m[0], m[1], m[2], m[8], m[9], m[10], m[4], m[5], m[6]);
        observer.orientation = observer.orbitalFrame().multiply(camera_matrix);
    }

    // ── Animation loop ──

    var lastTimestamp = Date.now();

    function getFrameDuration() {
        var now = Date.now();
        var diff = (now - lastTimestamp) / 1000.0;
        lastTimestamp = now;
        return Math.min(diff, 0.1); // cap at 100ms to avoid jumps
    }

    function animate() {
        requestAnimationFrame(animate);
        render();
    }

    function render() {
        observer.move(getFrameDuration());
        updateCamera();
        updateUniforms();
        renderer.render(scene, camera);
    }

    // ── Start ──

    try {
        // Check WebGL support
        var testCanvas = document.createElement('canvas');
        var gl = testCanvas.getContext('webgl') || testCanvas.getContext('experimental-webgl');
        if (!gl) throw new Error('WebGL not supported');

        loadTexture('stars', '/static/img/stars.png', THREE.NearestFilter);
        loadTexture('accretion_disk', '/static/img/accretion-disk.png', THREE.LinearFilter);
        loadAcidburnGalaxy();
        loadTexture('spectra', '/static/img/spectra.png', THREE.LinearFilter);
    } catch (e) {
        console.warn('[Blackhole] WebGL init failed, using CSS fallback:', e);
        container.style.background = 'radial-gradient(ellipse at 50% 50%, #0a0015 0%, #05000a 40%, #000 100%)';
    }
})();
