/* Snowwork 3D. All snow geometry is driven by packaged AvaFrame FT/FV fields. */
const C = window.Cesium;
const $ = (id) => document.getElementById(id);
const state = { manifest: null, time: 0, playing: false, speed: 2, mode: 'snow', heightScale: 3,
  vertical: 1.5, zone: null, zones: null, scenario: null, loaded: false, frameIndex: -1 };
let viewer, terrainPrimitive, snowPrimitive, particlePrimitive, overlays = [], loadToken = 0, lastTick = 0, lastUi = 0, toastTimer, jobPoll;
const modelMatrix = C.Transforms.eastNorthUpToFixedFrame(C.Cartesian3.fromDegrees(0, 0, 0));
const inverseModelMatrix = C.Matrix4.inverseTransformation(modelMatrix, new C.Matrix4());
const orbit = { target: new C.Cartesian3(-400, 0, 550), heading: C.Math.toRadians(310), pitch: C.Math.toRadians(-37), range: 4800 };

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5500);
}
function clock(seconds, precise = false) {
  const min = Math.floor(seconds / 60);
  const sec = seconds - min * 60;
  return `${String(min).padStart(2, '0')}:${sec.toFixed(precise ? 1 : 0).padStart(precise ? 4 : 2, '0')}`;
}
async function getJSON(url, options) {
  const response = await fetch(url, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败：${response.status}`);
  return value;
}
async function getBinary(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`数据读取失败：${response.status}`);
  return response.arrayBuffer();
}
// The winter Sentinel-2 crop is loaded as an image and uploaded into a Cesium
// texture on first use, so the page still works from the cache-free file path.
async function loadAlbedo(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = 'async';
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`地形影像加载失败：${url}`));
    image.src = url;
  });
}

const vertexShader = `
in vec3 position;
in vec3 normal;
in vec2 st;
out vec3 v_normal;
out vec3 v_local;
out vec2 v_uv;
out float v_height;
out float v_speed;
out float v_bump;
uniform float u_vertical;
float vhash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}
float vnoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(vhash(i), vhash(i + vec2(1.0, 0.0)), u.x),
               mix(vhash(i + vec2(0.0, 1.0)), vhash(i + vec2(1.0, 1.0)), u.x), u.y);
}
// Rendering-only micro-relief. The mesh already carries real curvature baked at
// pack time; this only adds the sub-mesh grain that a 15 m triangle cannot hold.
float microRelief(vec2 xy, vec3 nrm) {
    float slope = clamp(1.0 - nrm.z, 0.0, 1.0);
    float medium = vnoise(xy * 0.045) - 0.5;      // ~22 m
    float fine = vnoise(xy * 0.11) - 0.5;         // ~9 m
    return (medium * 0.35 + fine * 0.18) * slope;
}
#ifdef SNOW
uniform sampler2D u_frameA;
uniform sampler2D u_frameB;
uniform float u_mix;
uniform float u_heightScale;
uniform float u_speedScale;
uniform float u_exaggeration;
vec2 decodeState(vec4 rgba) {
    return vec2(dot(rgba.rg, vec2(256.0, 1.0)), dot(rgba.ba, vec2(256.0, 1.0))) * (255.0 / 65535.0);
}
#endif
void main() {
    vec3 p = position;
    v_height = 0.0;
    v_speed = 0.0;
    // Both surfaces share the same synthesised terrain texture, so the snow keeps
    // sitting exactly on the ground underneath it.
    float relief = microRelief(position.xy, normal);
    p.z += relief;
#ifdef SNOW
    vec2 a = decodeState(texture(u_frameA, st));
    vec2 b = decodeState(texture(u_frameB, st));
    vec2 value = mix(a, b, u_mix);
    v_height = value.x * u_heightScale;
    v_speed = value.y * u_speedScale;
    // Loose snow is not a smooth offset of the ground: displace the surface with
    // two octaves of noise so the mass has real lumps, ribs and a ragged rim.
    // Frequencies stay above the mesh Nyquist limit (15 m triangles -> >=30 m).
    float cov = smoothstep(0.008, 0.42, v_height);
    float lumps = (vnoise(position.xy * 0.012) - 0.5) * 2.6;
    float packs = (vnoise(position.xy * 0.030) - 0.5) * 1.0;
    v_bump = (lumps + packs) * cov;
    p += normal * (v_height * u_exaggeration + 0.2 + v_bump);
#else
    v_bump = 0.0;
#endif
    // Display-only vertical stretch: terrain and snow share it, so the snow
    // surface stays glued to the DEM. Packaged data stays in raw metres.
    p.z *= u_vertical;
    v_normal = normal;
    v_local = p;
    v_uv = st;
    gl_Position = czm_modelViewProjection * vec4(p, 1.0);
}`;

const fragmentShader = `
in vec3 v_normal;
in vec3 v_local;
in vec2 v_uv;
in float v_height;
in float v_speed;
in float v_bump;
uniform float u_mode;
uniform float u_heightScale;
uniform float u_speedScale;
uniform float u_vertical;
uniform float u_relief;
uniform float u_time;
uniform sampler2D u_albedo;
uniform float u_hasAlbedo;
vec3 ramp(float value) {
    value = clamp(value, 0.0, 1.0);
    vec3 cold = mix(vec3(.23,.61,.77), vec3(.45,.86,.79), smoothstep(0.0,.35,value));
    vec3 warm = mix(vec3(.91,.88,.53), vec3(.94,.40,.34), smoothstep(.62,1.0,value));
    return mix(cold, warm, smoothstep(.30,.66,value));
}
float hash12(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}
float valueNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash12(i), hash12(i + vec2(1.0, 0.0)), u.x),
               mix(hash12(i + vec2(0.0, 1.0)), hash12(i + vec2(1.0, 1.0)), u.x), u.y);
}
void main() {
    // The stored normals describe the raw DEM; stretching z rotates them.
    vec3 n = normalize(vec3(v_normal.xy * u_vertical, v_normal.z));
    vec3 light = normalize(vec3(-.42, -.46, .78));
    vec3 color;
    float alpha = 1.0;
#ifdef SNOW
    // The outline follows the interpolated height field, not the texel grid, so
    // the avalanche silhouette is a smooth contour instead of a staircase.
    float coverage = smoothstep(0.008, 0.42, v_height);
    if (coverage <= 0.001) discard;

    // Smooth base normal, tilted by the slope of the displaced surface so the
    // lumps cast real light and shade instead of reading as a flat sheet.
    vec3 nSnow = normalize(n + vec3(-dFdx(v_bump), -dFdy(v_bump), 0.0) * 3.0);

    float lambert = max(dot(nSnow, light), 0.0);
    float sky = 0.5 + 0.5 * nSnow.z;
    // Strong contrast: snow in shadow goes cold blue-grey, sunlit faces stay
    // white. Without this the lumps read as one flat sheet of white.
    vec3 lit = mix(vec3(0.30, 0.40, 0.56), vec3(1.0), pow(lambert, 0.75));
    lit = mix(lit, vec3(1.0), sky * 0.12);

    if (u_mode > .5 && u_mode < 1.5) color = ramp(v_speed / u_speedScale);
    else if (u_mode > 1.5) color = ramp(v_height / u_heightScale);
    else color = mix(vec3(0.93, 0.96, 1.0), vec3(1.0), smoothstep(0.2, 2.0, v_height));
    color *= lit;
    // Thicker snow reads denser and slightly brighter than the thin tongue.
    color *= 0.90 + 0.10 * smoothstep(0.0, 2.2, v_height);

    // The advancing lip: where the depth rises fastest the snow piles up and
    // catches the light, which is what makes a front read as a front.
    float shoulder = length(vec2(dFdx(v_height), dFdy(v_height)));
    float lip = smoothstep(0.015, 0.20, shoulder) * coverage;
    color += vec3(0.14, 0.18, 0.22) * lip;

    vec3 viewDir = normalize(vec3(0.2, 0.35, 0.91));
    color += pow(max(dot(reflect(-light, nSnow), viewDir), 0.0), 28.0) * vec3(0.26);

    // Solid snow: fully opaque everywhere except a hairline feather at the rim,
    // so the body reads as a mass instead of a sheet of glass.
    alpha = mix(0.62, 1.0, smoothstep(0.004, 0.05, v_height));
#else
    vec3 albedo = vec3(.80,.85,.90);
    if (u_hasAlbedo > .5) albedo = texture(u_albedo, v_uv).rgb;
    // Snow ground: warm-white where the sun hits, cold blue in the hollows. That
    // contrast is what lets a white avalanche read against a white mountain.
    float lambert = max(dot(n, light), 0.0);
    vec3 lit = mix(vec3(0.44, 0.54, 0.70), vec3(1.0, 1.0, 1.0), lambert * 0.80 + 0.20);
    float upness = clamp(n.z * 0.5 + 0.5, 0.0, 1.0);
    lit = mix(lit, vec3(0.98, 0.99, 1.0), upness * 0.28);
    color = albedo * lit * 0.92;
    // A touch of aerial perspective with distance keeps the ridge readable.
    float distance = length(v_local.xy) / max(u_relief * 9.0, 1.0);
    color = mix(color, vec3(.36,.47,.58), clamp(distance, 0.0, 1.0) * .28);
    float contour = 1.0 - smoothstep(.0, .028, abs(fract(v_local.z / max(u_relief, 1.0) * 12.0) - .5));
    color *= 1.0 - contour * .08;
#endif
    out_FragColor = vec4(color, alpha);
}`;

class FieldMesh {
  constructor(manifest, terrain, snow = false, binary = null) {
    this.manifest = manifest;
    this.terrain = terrain;
    this.isSnow = snow;
    this.binary = binary;
    this.textures = new Map();
    this.mix = 0;
    this.mode = 0;
    this.exaggeration = 3;
    this.vertical = 1;
    this.time = 0;
    this.albedo = null;
    this.albedoImage = null;
    this.drawSurface = true;
    this.show = true;
    this.command = null;
    this.pair = null;
    this.frameA = null;
    this.frameB = null;
  }
  setPair(a, b) { this.pair = [a, b]; }
  texture(context, index) {
    if (this.textures.has(index)) return this.textures.get(index);
    const m = this.manifest;
    const buffer = new Uint8Array(this.binary, index * m.frameBytes, m.frameBytes);
    const texture = new C.Texture({ context, width: m.width, height: m.height,
      pixelFormat: C.PixelFormat.RGBA, pixelDatatype: C.PixelDatatype.UNSIGNED_BYTE,
      source: { width: m.width, height: m.height, arrayBufferView: buffer }, flipY: false,
      sampler: new C.Sampler({ wrapS: C.TextureWrap.CLAMP_TO_EDGE, wrapT: C.TextureWrap.CLAMP_TO_EDGE,
        minificationFilter: C.TextureMinificationFilter.LINEAR, magnificationFilter: C.TextureMagnificationFilter.LINEAR }) });
    this.textures.set(index, texture);
    return texture;
  }
  build(context) {
    // Geometry may be finer than the frame textures: meshWidth/Height describe the
    // vertices, width/height describe the depth/velocity images.
    const textureWidth = this.manifest.width, textureHeight = this.manifest.height;
    const width = this.manifest.meshWidth ?? textureWidth;
    const height = this.manifest.meshHeight ?? textureHeight;
    const count = width * height;
    const uv = new Float32Array(count * 2);
    for (let row = 0; row < height; row++) for (let col = 0; col < width; col++) {
      const offset = (row * width + col) * 2;
      uv[offset] = col / (width - 1 || 1);
      uv[offset + 1] = row / (height - 1 || 1);
    }
    const indices = new Uint32Array((width - 1) * (height - 1) * 6);
    let k = 0;
    for (let row = 0; row < height - 1; row++) for (let col = 0; col < width - 1; col++) {
      const a = row * width + col, b = a + 1, c = a + width, d = c + 1;
      indices[k++] = a; indices[k++] = c; indices[k++] = b;
      indices[k++] = b; indices[k++] = c; indices[k++] = d;
    }
    const vertexBuffer = C.Buffer.createVertexBuffer({ context, typedArray: this.terrain, usage: C.BufferUsage.STATIC_DRAW });
    if (this.albedoImage && !this.albedo) {
      // Row 0 of the Sentinel-2 crop is north, which is also v=0 in our UVs.
      // Plain linear filtering: the crop is non-power-of-two and Cesium does not
      // build mip chains itself, which would leave the sampler incomplete.
      this.albedo = new C.Texture({ context, source: this.albedoImage, flipY: false,
        sampler: new C.Sampler({ wrapS: C.TextureWrap.CLAMP_TO_EDGE, wrapT: C.TextureWrap.CLAMP_TO_EDGE,
          minificationFilter: C.TextureMinificationFilter.LINEAR,
          magnificationFilter: C.TextureMagnificationFilter.LINEAR }) });
    }
    if (!this.albedo) {
      // Sampler uniforms cannot stay unbound: synthetic scenes ship no imagery.
      this.albedo = new C.Texture({ context, width: 1, height: 1,
        pixelFormat: C.PixelFormat.RGBA, pixelDatatype: C.PixelDatatype.UNSIGNED_BYTE,
        source: { width: 1, height: 1, arrayBufferView: new Uint8Array([255, 255, 255, 255]) },
        flipY: false,
        sampler: new C.Sampler({ wrapS: C.TextureWrap.CLAMP_TO_EDGE, wrapT: C.TextureWrap.CLAMP_TO_EDGE,
          minificationFilter: C.TextureMinificationFilter.NEAREST,
          magnificationFilter: C.TextureMagnificationFilter.NEAREST }) });
    }
    const uvBuffer = C.Buffer.createVertexBuffer({ context, typedArray: uv, usage: C.BufferUsage.STATIC_DRAW });
    const indexBuffer = C.Buffer.createIndexBuffer({ context, typedArray: indices, usage: C.BufferUsage.STATIC_DRAW, indexDatatype: C.IndexDatatype.UNSIGNED_INT });
    this.vertexArray = new C.VertexArray({ context, attributes: [
      { index: 0, vertexBuffer, componentsPerAttribute: 3, componentDatatype: C.ComponentDatatype.FLOAT, offsetInBytes: 0, strideInBytes: 24 },
      { index: 1, vertexBuffer, componentsPerAttribute: 3, componentDatatype: C.ComponentDatatype.FLOAT, offsetInBytes: 12, strideInBytes: 24 },
      { index: 2, vertexBuffer: uvBuffer, componentsPerAttribute: 2, componentDatatype: C.ComponentDatatype.FLOAT },
    ], indexBuffer });
    const defines = this.isSnow ? ['SNOW'] : [];
    this.shaderProgram = C.ShaderProgram.fromCache({ context,
      vertexShaderSource: new C.ShaderSource({ sources: [vertexShader], defines }),
      fragmentShaderSource: new C.ShaderSource({ sources: [fragmentShader], defines }),
      attributeLocations: { position: 0, normal: 1, st: 2 } });
    const uniforms = {
      u_mode: () => this.mode,
      u_heightScale: () => this.manifest.heightScale,
      u_speedScale: () => this.manifest.speedScale,
      u_exaggeration: () => this.exaggeration,
      u_vertical: () => this.vertical,
      u_relief: () => Math.max((this.manifest.heightMax - this.manifest.heightMin) * this.vertical, 1),
      u_time: () => this.time,
      u_mix: () => this.mix,
      u_frameA: () => this.frameA,
      u_frameB: () => this.frameB,
      u_albedo: () => this.albedo,
      u_hasAlbedo: () => (this.albedo && !this.isSnow ? 1 : 0),
    };
    const relief = (this.manifest.heightMax - this.manifest.heightMin) * this.vertical;
    const spanX = this.manifest.bounds[2] - this.manifest.bounds[0];
    const spanY = this.manifest.bounds[3] - this.manifest.bounds[1];
    const radius = Math.max(Math.hypot(spanX, spanY) * 0.62, relief * 1.4, 50);
    const localSphere = new C.BoundingSphere(new C.Cartesian3(0, 0, relief / 2), radius);
    const renderState = this.isSnow
      ? C.RenderState.fromCache({ depthTest: { enabled: true, func: C.DepthFunction.LESS_OR_EQUAL },
          depthMask: false, blending: C.BlendingState.ALPHA_BLEND, cull: { enabled: false } })
      : C.RenderState.fromCache({ depthTest: { enabled: true, func: C.DepthFunction.LESS_OR_EQUAL },
          depthMask: true, cull: { enabled: false } });
    this.command = new C.DrawCommand({ owner: this, modelMatrix,
      vertexArray: this.vertexArray, shaderProgram: this.shaderProgram, uniformMap: uniforms,
      renderState,
      primitiveType: C.PrimitiveType.TRIANGLES, pass: this.isSnow ? C.Pass.TRANSLUCENT : C.Pass.OPAQUE,
      boundingVolume: C.BoundingSphere.transform(localSphere, modelMatrix), cull: false,
    });
  }
  update(frameState) {
    if (!this.show) return;
    if (!this.command) this.build(frameState.context);
    if (this.isSnow) {
      // The flow body is drawn as a particle mass; this mesh stays alive purely
      // to keep the depth/velocity textures uploaded and to hold the pair state.
      if (!this.pair) return;
      const [a, b] = this.pair;
      this.frameA = this.texture(frameState.context, a);
      this.frameB = this.texture(frameState.context, b);
      for (const [index, texture] of this.textures) if (index !== a && index !== b) {
        texture.destroy(); this.textures.delete(index);
      }
    }
    if (this.drawSurface) frameState.commandList.push(this.command);
  }
  isDestroyed() { return false; }
  destroy() {
    for (const texture of this.textures.values()) texture.destroy();
    this.textures.clear();
    if (this.vertexArray && !this.vertexArray.isDestroyed()) this.vertexArray.destroy();
    if (this.shaderProgram && !this.shaderProgram.isDestroyed()) this.shaderProgram.destroy();
    return C.destroyObject(this);
  }
}

/* Whole-terrain snow cloud. Every particle carries the moment the avalanche
   reached its cell, so snow rests quietly across the mountain and only starts
   moving where and when the flow actually arrives. */
const particleVertexShader = `
in vec3 position;
in vec3 normal;
in vec2 st;
in vec4 flow;      // arrivalTime, peakSpeed, downhillX, downhillY
in float seed;
out vec3 v_color;
out float v_alpha;
uniform sampler2D u_frameA;
uniform sampler2D u_frameB;
uniform float u_mix;
uniform float u_heightScale;
uniform float u_speedScale;
uniform float u_exaggeration;
uniform float u_vertical;
uniform float u_time;
uniform float u_size;
uniform float u_mode;
vec2 decodeState(vec4 rgba) {
    return vec2(dot(rgba.rg, vec2(256.0, 1.0)), dot(rgba.ba, vec2(256.0, 1.0))) * (255.0 / 65535.0);
}
vec3 ramp(float value) {
    value = clamp(value, 0.0, 1.0);
    vec3 cold = mix(vec3(.23,.61,.77), vec3(.45,.86,.79), smoothstep(0.0,.35,value));
    vec3 warm = mix(vec3(.91,.88,.53), vec3(.94,.40,.34), smoothstep(.62,1.0,value));
    return mix(cold, warm, smoothstep(.30,.66,value));
}
void main() {
    vec2 value = mix(decodeState(texture(u_frameA, st)), decodeState(texture(u_frameB, st)), u_mix);
    float depth = value.x * u_heightScale;
    float speed = value.y * u_speedScale;
    float arrived = (flow.x >= 0.0 && u_time >= flow.x) ? 1.0 : 0.0;
    float since = max(u_time - max(flow.x, 0.0), 0.0);
    float flowing = arrived * smoothstep(0.04, 0.55, depth);

    // Airborne powder: it rides above the flowing surface and drifts downslope
    // with the front. Nothing here contributes to the body - the body is the
    // shaded surface underneath.
    vec3 p = position;
    float thick = max(depth, 0.25) * u_exaggeration;
    float rise = 1.6 + flow.z * (thick * 1.6 + 7.0);
    p += normal * rise;
    vec2 downhill = normalize(-normal.xy + vec2(1.0e-4, 1.0e-4));
    p.xy += downhill * min(since, 10.0) * (2.0 + min(speed, 20.0) * 0.25) * flowing;
    p.z *= u_vertical;

    v_color = vec3(0.98, 0.99, 1.0);
    v_alpha = mix(0.04, 0.13, flowing) * (0.6 + 0.4 * flow.w);

    vec4 mv = czm_modelView * vec4(p, 1.0);
    gl_Position = czm_projection * mv;
    float sizeMul = 1.1 + flow.w * 1.6;
    gl_PointSize = clamp(u_size * sizeMul * 1900.0 / max(-mv.z, 1.0), 1.2, 26.0);
}`;

const particleFragmentShader = `
in vec3 v_color;
in float v_alpha;
void main() {
    float radius = length(gl_PointCoord - 0.5) * 2.0;
    if (radius > 1.0) discard;
    float soft = 1.0 - smoothstep(0.05, 1.0, radius);
    out_FragColor = vec4(v_color, soft * v_alpha);
}`;

class SnowParticles {
  constructor(manifest, buffer, albedo) {
    this.manifest = manifest;
    this.buffer = buffer;
    this.albedo = albedo;
    this.vertical = 1;
    this.time = 0;
    this.size = 3.2;
    this.mode = 0;
    this.show = true;
    this.command = null;
    this.pair = null;
    this.textureA = null;
    this.textureB = null;
  }
  setPair(a, b) { this.pair = [a, b]; }
  build(context) {
    const stride = this.manifest.particles.stride * 4;
    const buffer = C.Buffer.createVertexBuffer({ context, typedArray: new Float32Array(this.buffer),
      usage: C.BufferUsage.STATIC_DRAW });
    this.vertexArray = new C.VertexArray({ context, attributes: [
      { index: 0, vertexBuffer: buffer, componentsPerAttribute: 3, componentDatatype: C.ComponentDatatype.FLOAT, offsetInBytes: 0, strideInBytes: stride },
      { index: 1, vertexBuffer: buffer, componentsPerAttribute: 3, componentDatatype: C.ComponentDatatype.FLOAT, offsetInBytes: 12, strideInBytes: stride },
      { index: 2, vertexBuffer: buffer, componentsPerAttribute: 2, componentDatatype: C.ComponentDatatype.FLOAT, offsetInBytes: 24, strideInBytes: stride },
      { index: 3, vertexBuffer: buffer, componentsPerAttribute: 4, componentDatatype: C.ComponentDatatype.FLOAT, offsetInBytes: 32, strideInBytes: stride },
      { index: 4, vertexBuffer: buffer, componentsPerAttribute: 1, componentDatatype: C.ComponentDatatype.FLOAT, offsetInBytes: 48, strideInBytes: stride },
    ] });
    this.shaderProgram = C.ShaderProgram.fromCache({ context,
      vertexShaderSource: new C.ShaderSource({ sources: [particleVertexShader] }),
      fragmentShaderSource: new C.ShaderSource({ sources: [particleFragmentShader] }),
      attributeLocations: { position: 0, normal: 1, st: 2, flow: 3, seed: 4 } });
    const uniforms = {
      u_frameA: () => this.textureA,
      u_frameB: () => this.textureB,
      u_mix: () => this.mix,
      u_heightScale: () => this.manifest.heightScale,
      u_speedScale: () => this.manifest.speedScale,
      u_exaggeration: () => this.exaggeration,
      u_vertical: () => this.vertical,
      u_time: () => this.time,
      u_size: () => this.size,
      u_mode: () => this.mode,
    };
    const relief = (this.manifest.heightMax - this.manifest.heightMin) * this.vertical;
    const spanX = this.manifest.bounds[2] - this.manifest.bounds[0];
    const spanY = this.manifest.bounds[3] - this.manifest.bounds[1];
    const radius = Math.max(Math.hypot(spanX, spanY) * 0.62, relief * 1.4, 50);
    const sphere = new C.BoundingSphere(new C.Cartesian3(0, 0, relief / 2), radius);
    this.command = new C.DrawCommand({ owner: this, modelMatrix,
      vertexArray: this.vertexArray, shaderProgram: this.shaderProgram, uniformMap: uniforms,
      renderState: C.RenderState.fromCache({
        depthTest: { enabled: true, func: C.DepthFunction.LESS_OR_EQUAL }, depthMask: false,
        blending: C.BlendingState.ALPHA_BLEND, cull: { enabled: false } }),
      primitiveType: C.PrimitiveType.POINTS, pass: C.Pass.TRANSLUCENT,
      boundingVolume: C.BoundingSphere.transform(sphere, modelMatrix), cull: false,
    });
  }
  update(frameState) {
    if (!this.show || !this.pair) return;
    if (!this.command) this.build(frameState.context);
    const [a, b] = this.pair;
    const snow = snowPrimitive;
    this.textureA = snow.texture(frameState.context, a);
    this.textureB = snow.texture(frameState.context, b);
    this.mix = snow.mix;
    this.exaggeration = snow.exaggeration;
    this.mode = snow.mode;
    frameState.commandList.push(this.command);
  }
  isDestroyed() { return false; }
  destroy() {
    if (this.vertexArray && !this.vertexArray.isDestroyed()) this.vertexArray.destroy();
    if (this.shaderProgram && !this.shaderProgram.isDestroyed()) this.shaderProgram.destroy();
    return C.destroyObject(this);
  }
}

function createViewer() {
  viewer = new C.Viewer('cesium-container', {
    baseLayer: false, globe: false, skyBox: false, skyAtmosphere: false,
    geocoder: false, homeButton: false, sceneModePicker: false, baseLayerPicker: false,
    navigationHelpButton: false, animation: false, timeline: false, fullscreenButton: false,
    selectionIndicator: false, infoBox: false, shouldAnimate: false, requestRenderMode: false,
    contextOptions: { webgl: { alpha: true, antialias: true, preserveDrawingBuffer: true } },
  });
  viewer.scene.backgroundColor = C.Color.fromCssColorString('#101f2d');
  viewer.scene.fog.enabled = false;
  viewer.scene.highDynamicRange = false;
  viewer.scene.screenSpaceCameraController.minimumZoomDistance = 60;
  viewer.scene.screenSpaceCameraController.maximumZoomDistance = 48000;
  viewer.scene.screenSpaceCameraController.enableCollisionDetection = false;
  viewer.scene.screenSpaceCameraController.enableInputs = false;
  viewer.resolutionScale = Math.min(1, 1.5 / window.devicePixelRatio);
  viewer.scene.renderError.addEventListener((_scene, error) => {
    console.error(error);
    showError('三维渲染发生错误', error.message || String(error));
  });
  // Orbit the local simulated valley, rather than the globe's center.
  const canvas = viewer.scene.canvas;
  let drag = null;
  canvas.addEventListener('contextmenu', event => event.preventDefault());
  canvas.addEventListener('pointerdown', event => {
    canvas.setPointerCapture(event.pointerId);
    drag = { id:event.pointerId, button:event.button, x:event.clientX, y:event.clientY };
  });
  canvas.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    drag.x = event.clientX; drag.y = event.clientY;
    if (drag.button === 2) {
      const right = C.Matrix4.multiplyByPointAsVector(inverseModelMatrix, viewer.camera.rightWC, new C.Cartesian3());
      const up = C.Matrix4.multiplyByPointAsVector(inverseModelMatrix, viewer.camera.upWC, new C.Cartesian3());
      const scale = orbit.range / canvas.clientHeight * .9;
      orbit.target.x += (-dx * right.x + dy * up.x) * scale;
      orbit.target.y += (-dx * right.y + dy * up.y) * scale;
    } else {
      orbit.heading -= dx * .005;
      orbit.pitch = C.Math.clamp(orbit.pitch + dy * .004, -Math.PI / 2 + .003, -.06);
    }
    applyOrbit();
  });
  for (const event of ['pointerup','pointercancel','lostpointercapture']) canvas.addEventListener(event, () => { drag = null; });
  canvas.addEventListener('wheel', event => {
    event.preventDefault();
    orbit.range = C.Math.clamp(orbit.range * Math.exp(event.deltaY * .001), 100, 16000);
    applyOrbit();
  }, {passive:false});
}

function position(x, y, z) { return C.Matrix4.multiplyByPoint(modelMatrix, new C.Cartesian3(x, y, z), new C.Cartesian3()); }
function applyOrbit() {
  viewer.camera.lookAt(position(orbit.target.x, orbit.target.y, orbit.target.z), new C.HeadingPitchRange(orbit.heading, orbit.pitch, orbit.range));
  viewer.camera.lookAtTransform(C.Matrix4.IDENTITY);
}
function sceneFrame(manifest = state.manifest, vertical = state.vertical) {
  if (!manifest) return { cx: 0, cy: 0, spanX: 1200, spanY: 1200, span: 1200, relief: 100 };
  // Frame the ground the avalanche actually reached, not the whole DEM crop.
  const flow = manifest.flowBounds;
  let cx = 0, cy = 0, spanX, spanY;
  if (flow) {
    cx = (flow[0] + flow[2]) / 2;
    cy = (flow[1] + flow[3]) / 2;
    spanX = Math.max(flow[2] - flow[0], 1);
    spanY = Math.max(flow[3] - flow[1], 1);
  } else {
    spanX = manifest.bounds[2] - manifest.bounds[0];
    spanY = manifest.bounds[3] - manifest.bounds[1];
  }
  const relief = Math.max((manifest.heightMax - manifest.heightMin) * vertical, 1);
  return { cx, cy, spanX, spanY, span: Math.max(spanX, spanY), relief };
}
function setCamera(kind = 'oblique') {
  if (!state.manifest) return;
  const { cx, cy, spanX, spanY, span, relief } = sceneFrame();
  const close = kind === 'close';
  orbit.target = new C.Cartesian3(cx, cy, relief * (close ? 0.62 : 0.42));
  orbit.heading = C.Math.toRadians(kind === 'top' ? 0 : close ? 340 : 322);
  orbit.pitch = C.Math.toRadians(kind === 'top' ? -89.8 : close ? -19 : -36);
  orbit.range = kind === 'top'
    ? Math.max(spanX * 1.5, spanY * 1.05)
    : close ? span * 0.55 : span * 2.4;
  applyOrbit();
  $('view-top').classList.toggle('active', kind === 'top');
  $('view-close').classList.toggle('active', close);
  $('view-oblique').classList.toggle('active', kind !== 'top' && !close);
}

// Current snow thickness at a local (x,y) point, read straight out of the packed
// frame texture so the outlines can ride on top of the flowing surface.
function depthAt(x, y) {
  const m = state.manifest;
  if (!m || !snowPrimitive || !snowPrimitive.binary) return 0;
  const index = Math.max(0, state.frameIndex);
  const frame = m.frames[index];
  if (!frame) return 0;
  const west = m.bounds[0] - m.origin[0], east = m.bounds[2] - m.origin[0];
  const south = m.bounds[1] - m.origin[1], north = m.bounds[3] - m.origin[1];
  const u = (x - west) / Math.max(east - west, 1e-6);
  const v = (north - y) / Math.max(north - south, 1e-6);
  if (u < 0 || u > 1 || v < 0 || v > 1) return 0;
  const col = Math.min(m.width - 1, Math.max(0, Math.floor(u * m.width)));
  const row = Math.min(m.height - 1, Math.max(0, Math.floor(v * m.height)));
  const offset = ((index * m.height + row) * m.width + col) * 4;
  const bytes = new Uint8Array(snowPrimitive.binary, offset, 4);
  const height16 = bytes[0] * 256 + bytes[1];
  return (height16 / 65535) * m.heightScale;
}

function createOverlays(manifest) {
  for (const entity of overlays) viewer.entities.remove(entity);
  overlays = [];
  const runZone = manifest.zone?.id || null;
  for (const overlay of manifest.overlays) {
    if (overlay.kind === 'resistance' && manifest.scenario !== 'res') continue;
    // The release outline and the selected candidate are the same polygon once a
    // zone has been solved, so draw it once (as the candidate) instead of twice.
    if (overlay.kind === 'release' && runZone) continue;
    const candidate = overlay.kind === 'candidate';
    const selected = candidate && overlay.id === state.zone;
    const isRelease = candidate && overlay.id === runZone;
    const color = C.Color.fromCssColorString(
      candidate ? (selected ? '#ffd166' : '#7f93a8')
        : overlay.kind === 'release' ? '#ffc788' : '#9d9cfe');
    // Ride above the snow: terrain elevation + current thickness * display scale,
    // then the same vertical exaggeration the mesh applies in its shader.
    const lift = (point) =>
      (point[2] + depthAt(point[0], point[1]) * state.heightScale + 6) * state.vertical;
    const entity = viewer.entities.add({
      polyline: {
        positions: new C.CallbackProperty(
          () => overlay.points.map((p) => position(p[0], p[1], lift(p))), false),
        width: candidate ? (selected ? 3.6 : 1.4) : 2.5,
        material: new C.PolylineDashMaterialProperty({ color, dashLength: candidate ? 8 : 14 }),
        depthFailMaterial: color.withAlpha(.55) },
    });
    entity.overlayKind = overlay.kind;
    entity.candidateId = overlay.id || null;
    overlays.push(entity);
    // Labelling every candidate would bury the terrain; only the active zone
    // and the release outline get a caption.
    if (candidate && !selected) continue;
    const point = overlay.points[0];
    const text = isRelease ? `初始释放区 · ${overlay.id}`
      : candidate ? `高危起崩区 ${overlay.id}`
        : overlay.kind === 'release' ? '初始释放区' : '阻力区';
    const label = viewer.entities.add({
      position: new C.CallbackProperty(
        () => position(point[0], point[1], lift(point) + 12), false),
      label: { text,
        font: '12px Microsoft YaHei', fillColor: color, style: C.LabelStyle.FILL_AND_OUTLINE,
        outlineColor: C.Color.fromCssColorString('#10202b'), outlineWidth: 4,
        pixelOffset: new C.Cartesian2(0, -9), disableDepthTestDistance: Number.POSITIVE_INFINITY,
        scaleByDistance: new C.NearFarScalar(2000, 1, 20000, .6) },
    });
    label.overlayKind = overlay.kind;
    label.candidateId = overlay.id || null;
    overlays.push(label);
  }
  updateOverlays();
}
function updateOverlays() {
  const showRelease = $('show-release').checked;
  const showCandidates = $('show-candidates').checked;
  for (const entity of overlays) {
    if (entity.overlayKind === 'candidate') {
      entity.show = showCandidates || entity.candidateId === state.zone;
    } else {
      entity.show = showRelease;
    }
  }
}

function showError(title, message) {
  $('loading').hidden = false;
  $('loading-title').textContent = title;
  $('loading-message').textContent = message;
  $('retry-button').hidden = false;
  $('loading').querySelector('.spinner').hidden = true;
}

async function loadScenario(id) {
  const token = ++loadToken;
  state.playing = false;
  updatePlayButton();
  $('loading').hidden = false;
  $('loading').querySelector('.spinner').hidden = false;
  $('retry-button').hidden = true;
  $('loading-title').textContent = '正在载入物理计算结果';
  $('loading-message').textContent = '装载同一网格上的三维地形和连续时间帧';
  try {
    const manifest = await getJSON(`/data/${encodeURIComponent(id)}/manifest.json`);
    const requests = [
      getBinary(`/data/${encodeURIComponent(id)}/terrain.bin`),
      getBinary(`/data/${encodeURIComponent(id)}/frames.bin`),
      manifest.particles ? getBinary(manifest.particles.url) : Promise.resolve(null),
      manifest.albedo ? loadAlbedo(manifest.albedo) : Promise.resolve(null),
    ];
    const [terrainBuffer, framesBuffer, snowBuffer, albedoImage] = await Promise.all(requests);
    if (token !== loadToken) return;
    const meshVertices = (manifest.meshWidth ?? manifest.width) * (manifest.meshHeight ?? manifest.height);
    if (terrainBuffer.byteLength !== meshVertices * 6 * 4 || framesBuffer.byteLength !== manifest.frameBytes * manifest.frames.length) throw new Error('数据长度与清单不一致，请重新转换计算结果');
    if (snowBuffer && snowBuffer.byteLength !== manifest.particles.bytes) throw new Error('雪粒子数据长度与清单不一致，请重新转换计算结果');
    if (!viewer) createViewer();
    if (terrainPrimitive) viewer.scene.primitives.remove(terrainPrimitive);
    if (snowPrimitive) viewer.scene.primitives.remove(snowPrimitive);
    if (particlePrimitive) viewer.scene.primitives.remove(particlePrimitive);
    const terrain = new Float32Array(terrainBuffer);
    terrainPrimitive = viewer.scene.primitives.add(new FieldMesh(manifest, terrain));
    snowPrimitive = viewer.scene.primitives.add(new FieldMesh(manifest, terrain, true, framesBuffer));
    particlePrimitive = null;
    terrainPrimitive.albedoImage = albedoImage;
    terrainPrimitive.vertical = manifest.verticalExaggeration ?? 1.0;
    // The avalanche body is a shaded surface again; the whole-mountain particle
    // cloud is retired, so there is nothing else to upload.
    snowPrimitive.drawSurface = true;
    // Powder plume disabled: the soft sprites read as haze over the surface
    // rather than as snow. The avalanche is a solid shaded body only.
    particlePrimitive = null;
    if (false && snowBuffer) {
      particlePrimitive = viewer.scene.primitives.add(new SnowParticles(manifest, snowBuffer));
      particlePrimitive.show = $('show-particles').checked;
    }
    state.manifest = manifest;
    state.scenario = id;
    state.time = 0;
    state.frameIndex = -1;
    state.loaded = true;
    if (manifest.zone && manifest.zone.id) state.zone = manifest.zone.id;
    else if (manifest.candidates && manifest.candidates.length && !state.zone) state.zone = manifest.candidates[0].id;
    applyVertical(manifest.verticalExaggeration ?? 1.0);
    createOverlays(manifest);
    setCamera();
    $('scene-title').textContent = manifest.title
      || (manifest.scenario === 'res' ? '阻力区 · 雪崩过程' : '基准 · 雪崩过程');
    $('scene-subtitle').textContent = `${manifest.source}${manifest.zone ? ` · ${manifest.zone.id}` : ''}`
      + ` · 初始释放厚度 ${manifest.initialThickness.toFixed(1)} m`;
    $('grid-badge').textContent = `计算网格 ${manifest.nativeCellSize} m`;
    syncZoneCards();
    $('peak-depth').textContent = `全程峰值 ${manifest.peakDepth.toFixed(2)} m`;
    $('peak-speed').textContent = `全程峰值 ${manifest.peakSpeed.toFixed(2)} m/s`;
    $('timeline').max = manifest.duration;
    $('total-time').textContent = clock(manifest.duration, true);
    $('timeline-ticks').replaceChildren(...[0,.25,.5,.75,1].map((fraction) => {
      const tick = document.createElement('span'); tick.textContent = clock(manifest.duration * fraction); return tick;
    }));
    $('data-status').textContent = `${manifest.frames.length} 帧 · 约 2 s 采样 · 本地数据`;
    $('play-button').disabled = false;
    document.querySelectorAll('.scenario-card').forEach((card) => card.classList.toggle('selected', card.dataset.id === id));
    applyMode();
    updateFrame(true);
    $('loading').hidden = true;
    if (!lastTick) requestAnimationFrame(tick);
  } catch (error) {
    console.error(error);
    showError('暂时无法载入场景', error.message);
  }
}

function updateFrame(force = false) {
  const m = state.manifest;
  if (!m || !snowPrimitive) return;
  let lo = 0, hi = m.frames.length - 1;
  while (lo < hi) { const mid = Math.ceil((lo + hi) / 2); if (m.frames[mid].t <= state.time) lo = mid; else hi = mid - 1; }
  const a = lo, b = Math.min(a + 1, m.frames.length - 1);
  const interval = m.frames[b].t - m.frames[a].t;
  snowPrimitive.mix = interval ? Math.min(1, Math.max(0, (state.time - m.frames[a].t) / interval)) : 0;
  snowPrimitive.exaggeration = state.heightScale;
  snowPrimitive.time = state.time;
  snowPrimitive.setPair(a, b);
  if (particlePrimitive) {
    particlePrimitive.setPair(a, b);
    particlePrimitive.time = state.time;
  }
  const nearest = snowPrimitive.mix < .5 ? a : b;
  if (force || state.frameIndex !== nearest) {
    state.frameIndex = nearest;
    const frame = m.frames[nearest];
    $('metric-depth').textContent = frame.maxDepth.toFixed(2);
    $('metric-speed').textContent = frame.maxSpeed.toFixed(2);
    $('metric-area').textContent = (frame.area / 10000).toFixed(2);
    $('frame-label').textContent = `第 ${nearest + 1} / ${m.frames.length} 帧 · 采样 ${frame.t.toFixed(1)} s`;
  }
  $('timeline').value = state.time;
  $('current-time').textContent = clock(state.time, true);
}
function tick(now) {
  const delta = lastTick ? Math.min((now - lastTick) / 1000, .2) : 0;
  lastTick = now;
  if (state.loaded && state.playing) {
    state.time = Math.min(state.manifest.duration, state.time + delta * state.speed);
    if (state.time >= state.manifest.duration) { state.playing = false; updatePlayButton(); }
  }
  if (now - lastUi > 40) { updateFrame(); lastUi = now; }
  requestAnimationFrame(tick);
}
function updatePlayButton() {
  $('play-button').textContent = state.playing ? 'Ⅱ' : '▶';
  $('play-button').classList.toggle('playing', state.playing);
  $('play-button').setAttribute('aria-label', state.playing ? '暂停雪崩' : '播放雪崩');
  $('playback-label').textContent = state.playing ? '正在播放物理过程' : '物理结果回放';
}
function applyMode() {
  if (!snowPrimitive) return;
  snowPrimitive.mode = { snow: 0, speed: 1, depth: 2 }[state.mode];
  $('legend-title').textContent = state.mode === 'speed' ? '流速 / m·s⁻¹' : '流深 / m';
  $('legend-max').textContent = (state.mode === 'speed' ? state.manifest.speedScale : state.manifest.heightScale).toFixed(1);
  $('legend').classList.toggle('snow', state.mode === 'snow');
}

function applyVertical(value) {
  // Vertical exaggeration is a display transform only: the shaders stretch z,
  // while analysis and the solver keep the raw elevations from the DEM.
  state.vertical = Math.min(3, Math.max(1, Number(value) || 1));
  $('vertical-scale').value = String(state.vertical);
  $('vertical-scale-value').textContent = `${state.vertical.toFixed(1)}×`;
  $('vertical-badge').textContent = `纵向 ${state.vertical.toFixed(1)}×`;
  if (terrainPrimitive) terrainPrimitive.vertical = state.vertical;
  if (snowPrimitive) snowPrimitive.vertical = state.vertical;
  if (particlePrimitive) particlePrimitive.vertical = state.vertical;
  if (state.manifest) {
    const relief = Math.max((state.manifest.heightMax - state.manifest.heightMin) * state.vertical, 1);
    orbit.target = new C.Cartesian3(orbit.target.x, orbit.target.y, relief * 0.42);
    applyOrbit();
  }
}

function syncZoneCards() {
  document.querySelectorAll('.zone-card').forEach((card) =>
    card.classList.toggle('selected', card.dataset.zone === state.zone));
  const select = $('compute-zone');
  if (state.zone && select.querySelector(`option[value="${state.zone}"]`)) select.value = state.zone;
}

function selectZone(id) {
  state.zone = id;
  syncZoneCards();
  if (state.manifest) createOverlays(state.manifest);
  // The highlighted candidate is only simulated after a compute request; the
  // visible result still belongs to the zone that was actually solved.
  const solved = state.manifest?.zone?.id;
  if (solved && id !== solved) toast(`已选 ${id}；点「用该起崩区计算」才会模拟该区(当前画面仍为 ${solved})`);
}

async function refreshZones() {
  try {
    const summary = await getJSON('/api/zones');
    state.zones = summary;
    const list = summary.candidates || [];
    $('zone-count').textContent = String(list.length).padStart(2, '0');
    if (!list.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-scenarios';
      empty.textContent = '尚未识别到候选起崩区';
      $('zone-list').replaceChildren(empty);
      return;
    }
    const nodes = list.map((zone) => {
      const button = document.createElement('button');
      button.className = `scenario-card zone-card${zone.rank === 1 ? ' high-risk' : ''}`
        + `${state.zone === zone.id ? ' selected' : ''}`;
      button.dataset.zone = zone.id;
      const dot = document.createElement('span'); dot.className = 'scenario-dot';
      const title = document.createElement('strong');
      title.textContent = zone.rank === 1 ? `高危起崩区 ${zone.id}` : `起崩区 ${zone.id}`;
      const subtitle = document.createElement('small');
      subtitle.textContent = `${(zone.areaM2 / 10000).toFixed(1)} ha · 均坡度 ${zone.meanSlopeDeg.toFixed(1)}°`
        + ` · 得分 ${zone.rankScore.toFixed(2)}`;
      const meta = document.createElement('span'); meta.className = 'scenario-meta';
      const rank = document.createElement('span');
      rank.textContent = `地形筛查 No.${String(zone.rank).padStart(2, '0')}`;
      const badge = document.createElement('span'); badge.className = 'ready';
      badge.textContent = zone.rank === 1 ? '最高危 ↗' : '候选';
      meta.append(rank, badge);
      button.append(dot, title, subtitle, meta);
      button.addEventListener('click', () => selectZone(zone.id));
      return button;
    });
    $('zone-list').replaceChildren(...nodes);
    const select = $('compute-zone');
    select.replaceChildren(...list.map((zone) => {
      const option = document.createElement('option');
      option.value = zone.id;
      option.textContent = `${zone.id} · ${(zone.areaM2 / 10000).toFixed(1)} ha · ${zone.meanSlopeDeg.toFixed(1)}°`;
      return option;
    }));
    if (!state.zone) state.zone = list[0].id;
    select.value = state.zone;
    if (summary.warning) $('zone-note').textContent = summary.warning;
  } catch (error) {
    $('zone-note').textContent = `起崩区识别数据不可用：${error.message}`;
  }
}

async function refreshScenarios(preferred) {
  const scenarios = await getJSON('/api/scenarios');
  $('scenario-count').textContent = String(scenarios.length).padStart(2, '0');
  const nodes = scenarios.map((item) => {
    const button = document.createElement('button');
    button.className = `scenario-card${state.scenario === item.id ? ' selected' : ''}`;
    button.dataset.id = item.id;
    const dot = document.createElement('span'); dot.className = 'scenario-dot';
    const title = document.createElement('strong');
    title.textContent = item.title
      || (item.scenario === 'res' ? '阻力区 · 衰减对照' : '基准 · 自由下滑');
    const subtitle = document.createElement('small');
    subtitle.textContent = item.zone
      ? `${item.zone.id} · 释放厚度 ${item.initialThickness.toFixed(1)} m · ${item.duration.toFixed(1)} s`
      : `释放厚度 ${item.initialThickness.toFixed(1)} m · ${item.duration.toFixed(1)} s 过程`;
    const meta = document.createElement('span'); meta.className = 'scenario-meta';
    const source = document.createElement('span');
    source.textContent = item.terrain === 'jiangjunshan' ? '将军山 / com1DFA' : '合成地形 / com1DFA';
    const ready = document.createElement('span'); ready.className = 'ready'; ready.textContent = '已计算 ↗';
    meta.append(source, ready); button.append(dot, title, subtitle, meta);
    button.addEventListener('click', () => { if (state.scenario !== item.id) loadScenario(item.id); });
    return button;
  });
  $('scenario-list').replaceChildren(...nodes);
  if (!scenarios.length) {
    showError('尚未生成时序数据', '请等待首次计算完成，或展开左侧“新建计算”提交一个情景。');
    $('loading').style.pointerEvents = 'none';
    return;
  }
  if (preferred || !state.loaded) {
    const fallback = scenarios.find((item) => item.default) || scenarios[0];
    await loadScenario(preferred || fallback.id);
  }
}

async function watchJob(id) {
  clearTimeout(jobPoll);
  $('job-panel').hidden = false;
  $('job-id').textContent = id;
  $('run-button').disabled = true;
  try {
    const job = await getJSON(`/api/jobs/${id}`);
    $('job-message').textContent = job.message || '等待计算进程启动';
    $('job-title').textContent = { queued:'等待启动', preparing:'正在准备', computing:'物理计算中', packing:'转换三维时序', complete:'计算完成', failed:'计算失败' }[job.phase] || '计算中';
    if (job.phase === 'complete') {
      $('run-button').disabled = false;
      $('job-panel').hidden = true;
      await refreshScenarios(id);
      toast('新情景计算完成，已载入三维场景');
      return;
    }
    if (job.phase === 'failed') {
      $('run-button').disabled = false;
      $('job-panel').querySelector('.spinner').hidden = true;
      toast(`计算失败：${job.message}`);
      return;
    }
    jobPoll = setTimeout(() => watchJob(id), 2000);
  } catch (error) {
    $('run-button').disabled = false;
    $('job-panel').hidden = true;
    toast(error.message);
  }
}

$('play-button').addEventListener('click', () => {
  if (!state.loaded) return;
  if (state.time >= state.manifest.duration) state.time = 0;
  state.playing = !state.playing;
  updatePlayButton();
});
$('restart-button').addEventListener('click', () => { state.time = 0; updateFrame(true); });
$('timeline').addEventListener('input', (event) => { state.time = Number(event.target.value); updateFrame(); });
$('playback-speed').addEventListener('change', (event) => { state.speed = Number(event.target.value); });
$('height-scale').addEventListener('input', (event) => { state.heightScale = Number(event.target.value); $('height-scale-value').textContent = `${state.heightScale}×`; updateFrame(); });
$('vertical-scale').addEventListener('input', (event) => applyVertical(event.target.value));
$('render-modes').addEventListener('click', (event) => {
  const button = event.target.closest('[data-mode]');
  if (!button) return;
  state.mode = button.dataset.mode;
  for (const sibling of button.parentElement.children) sibling.classList.toggle('selected', sibling === button);
  applyMode();
});
$('show-release').addEventListener('change', updateOverlays);
$('show-candidates').addEventListener('change', updateOverlays);
$('show-particles').addEventListener('change', () => {
  if (particlePrimitive) particlePrimitive.show = $('show-particles').checked;
});
$('view-oblique').addEventListener('click', () => setCamera());
$('view-close').addEventListener('click', () => setCamera('close'));
$('view-top').addEventListener('click', () => setCamera('top'));
$('view-reset').addEventListener('click', () => setCamera());
$('retry-button').addEventListener('click', () => window.location.reload());
$('toggle-compute').addEventListener('click', () => {
  $('compute-form').hidden = !$('compute-form').hidden;
  $('toggle-compute').textContent = $('compute-form').hidden ? '展开 ＋' : '收起 −';
  $('toggle-compute').setAttribute('aria-expanded', String(!$('compute-form').hidden));
});
$('compute-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    $('run-button').disabled = true;
    $('job-panel').querySelector('.spinner').hidden = false;
    const job = await getJSON('/api/run', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ terrain:'jiangjunshan', zone:$('compute-zone').value,
        thickness:Number($('thickness').value) }) });
    watchJob(job.id);
  } catch (error) { $('run-button').disabled = false; toast(error.message); }
});
window.addEventListener('keydown', (event) => {
  if (event.code === 'Space' && !['INPUT','SELECT','BUTTON'].includes(document.activeElement.tagName)) {
    event.preventDefault(); $('play-button').click();
  }
});
window.__snowwork = { state, get viewer() { return viewer; }, get particles() { return particlePrimitive; },
  loadScenario, setCamera };

try {
  const status = await getJSON('/api/status');
  $('engine-status').textContent = 'AvaFrame 本地引擎';
  if (!status.ready) throw new Error('Cesium 本地依赖尚未准备好，请执行 start.cmd 安装依赖');
  await refreshZones();
  await refreshScenarios();
  if (status.activeJob) watchJob(status.activeJob);
} catch (error) {
  $('engine-status').textContent = '连接异常';
  showError('无法启动三维实验室', error.message);
}
