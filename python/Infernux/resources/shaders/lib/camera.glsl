ShaderInfo {
    Name "Lib Camera"
}

// ============================================================================
// lib/camera.glsl — Camera and screen-space utility functions
//
// Requires: shader-linker camera contract plus InfGlobals for screen/depth data
// Usage: ShaderInfo Imports: Lib Camera
// ============================================================================

// ---- Screen coordinates ----

// Normalized screen UV (0~1, bottom-left origin)
vec2 getScreenUV() {
    return gl_FragCoord.xy * _Globals._ScreenParams.zw;
}

// Integer pixel coordinate
ivec2 getPixelCoord() {
    return ivec2(gl_FragCoord.xy);
}

// ---- Camera ----

// Structured shaders receive a camera source selected by the shader linker.
// Lit passes resolve this to the camera-local set-1 LightingUBO; unlit passes
// retain the engine-globals fallback. Never read _WorldSpaceCameraPos directly
// here: that buffer is shared by Scene and Game graphs and is staged from the
// editor camera.
#ifndef INX_SHADING_CAMERA_POSITION
#define INX_SHADING_CAMERA_POSITION _Globals._WorldSpaceCameraPos.xyz
#endif

// World-space camera position
vec3 getCameraPosition() {
    return INX_SHADING_CAMERA_POSITION;
}

// Normalized view direction from camera to fragment (world-space)
vec3 getViewDirection(vec3 worldPos) {
    return normalize(INX_SHADING_CAMERA_POSITION - worldPos);
}

// Camera near plane distance
float getCameraNear() {
    return _Globals._ProjectionParams.x;
}

// Camera far plane distance
float getCameraFar() {
    return _Globals._ProjectionParams.y;
}

// ---- Depth linearization ----

// Linearize depth buffer value to view-space distance (eye depth)
// rawDepth: value sampled from depth buffer [0,1]
// Returns positive eye-space distance
float linearEyeDepth(float rawDepth) {
    // _ZBufferParams: x = 1 - far/near, y = far/near, z = x/far, w = y/far
    return 1.0 / (_Globals._ZBufferParams.z * rawDepth + _Globals._ZBufferParams.w);
}

// Linearize depth to [0,1] range (0 = near, 1 = far)
float linear01Depth(float rawDepth) {
    return linearEyeDepth(rawDepth) / _Globals._ProjectionParams.y;
}

// ---- Time helpers ----

// Elapsed time in seconds
float getTime() {
    return _Globals._Time.x;
}

// Delta time (seconds since last frame)
float getDeltaTime() {
    return _Globals._Time.w;
}

// Smooth delta time (exponentially smoothed)
float getSmoothDeltaTime() {
    return _Globals._FrameParams.y;
}
