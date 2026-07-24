@shader_id: Lib Color

// ============================================================================
// lib/color.glsl — Color space conversions and adjustments
//
// Full-coverage color utilities matching Unity ShaderGraph node library.
// Provides: sRGB/linear, HSV/HSL, brightness, contrast, saturation,
// white balance, channel mixer, color mask, invert, replace color,
// channel split/combine, tone mapping.
// Usage: @import: Lib Color
// ============================================================================

// ============================================================================
// Color Space Conversions  (Unity: Colorspace Conversion)
// ============================================================================

// sRGB → Linear  (Unity: Colorspace Conversion, sRGB→Linear)
vec3 sRGBToLinear(vec3 srgb) {
    return mix(
        srgb / 12.92,
        pow((srgb + 0.055) / 1.055, vec3(2.4)),
        step(vec3(0.04045), srgb)
    );
}
vec4 sRGBToLinear(vec4 srgba) {
    return vec4(sRGBToLinear(srgba.rgb), srgba.a);
}

// Linear → sRGB  (Unity: Colorspace Conversion, Linear→sRGB)
vec3 linearToSRGB(vec3 linear) {
    return mix(
        linear * 12.92,
        1.055 * pow(linear, vec3(1.0 / 2.4)) - 0.055,
        step(vec3(0.0031308), linear)
    );
}
vec4 linearToSRGB(vec4 lina) {
    return vec4(linearToSRGB(lina.rgb), lina.a);
}

// ---- HSV ↔ RGB ----  (Unity: Colorspace Conversion, RGB↔HSV)

vec3 rgbToHSV(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

vec3 hsvToRGB(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

// ---- HSL ↔ RGB ----  (Unity: Colorspace Conversion, RGB↔HSL)

float _hue2rgb(float p, float q, float t) {
    if (t < 0.0) t += 1.0;
    if (t > 1.0) t -= 1.0;
    if (t < 1.0 / 6.0) return p + (q - p) * 6.0 * t;
    if (t < 1.0 / 2.0) return q;
    if (t < 2.0 / 3.0) return p + (q - p) * (2.0 / 3.0 - t) * 6.0;
    return p;
}

vec3 hslToRGB(vec3 hsl) {
    if (hsl.y <= 0.0) return vec3(hsl.z);
    float q = hsl.z < 0.5 ? hsl.z * (1.0 + hsl.y) : hsl.z + hsl.y - hsl.z * hsl.y;
    float p = 2.0 * hsl.z - q;
    return vec3(
        _hue2rgb(p, q, hsl.x + 1.0 / 3.0),
        _hue2rgb(p, q, hsl.x),
        _hue2rgb(p, q, hsl.x - 1.0 / 3.0)
    );
}

vec3 rgbToHSL(vec3 c) {
    float maxC = max(c.r, max(c.g, c.b));
    float minC = min(c.r, min(c.g, c.b));
    float l = (maxC + minC) * 0.5;
    if (maxC == minC) return vec3(0.0, 0.0, l);
    float d = maxC - minC;
    float s = l > 0.5 ? d / (2.0 - maxC - minC) : d / (maxC + minC);
    float h;
    if (maxC == c.r)      h = (c.g - c.b) / d + (c.g < c.b ? 6.0 : 0.0);
    else if (maxC == c.g) h = (c.b - c.r) / d + 2.0;
    else                  h = (c.r - c.g) / d + 4.0;
    h /= 6.0;
    return vec3(h, s, l);
}

// ============================================================================
// Adjustments  (Unity: Artistic > Adjustment category)
// ============================================================================

// Brightness  (Unity: not a standalone node, but common)
vec3 adjustBrightness(vec3 color, float amount) {
    return color + amount;
}

// Contrast  (Unity: Contrast)
vec3 adjustContrast(vec3 color, float contrast) {
    float midpoint = 0.5;
    return (color - midpoint) * contrast + midpoint;
}

// Saturation  (Unity: Saturation)
vec3 adjustSaturation(vec3 color, float saturation) {
    float lum = dot(color, vec3(0.2126, 0.7152, 0.0722));
    return mix(vec3(lum), color, saturation);
}

// Hue Shift (in radians)  (Unity: Hue)
vec3 hueShift(vec3 color, float shift) {
    vec3 hsv = rgbToHSV(color);
    hsv.x = fract(hsv.x + shift / 6.28318530718);
    return hsvToRGB(hsv);
}

// Hue Shift in degrees  (Unity: Hue — degrees mode)
vec3 hueShiftDegrees(vec3 color, float degrees) {
    vec3 hsv = rgbToHSV(color);
    hsv.x = fract(hsv.x + degrees / 360.0);
    return hsvToRGB(hsv);
}

// White Balance (temperature + tint)  (Unity: White Balance)
vec3 whiteBalance(vec3 color, float temperature, float tint) {
    // Attempt a simple approximation via color offset
    float t = temperature / 100.0;
    float ti = tint / 100.0;
    // Warm shifts red up / blue down; tint shifts green
    color.r += t;
    color.b -= t;
    color.g += ti;
    return max(color, vec3(0.0));
}

// Channel Mixer  (Unity: Channel Mixer)
vec3 channelMixer(vec3 color, vec3 redOut, vec3 greenOut, vec3 blueOut) {
    return vec3(
        dot(color, redOut),
        dot(color, greenOut),
        dot(color, blueOut)
    );
}

// Invert Colors  (Unity: Invert Colors)
vec3 invertColors(vec3 color) {
    return vec3(1.0) - color;
}
vec4 invertColors(vec4 color) {
    return vec4(vec3(1.0) - color.rgb, color.a);
}

// Color Mask — returns 1.0 where color matches maskColor within range  (Unity: Color Mask)
float colorMask(vec3 color, vec3 maskColor, float range, float fuzziness) {
    float dist = distance(color, maskColor);
    return 1.0 - clamp((dist - range) / max(fuzziness, 0.001), 0.0, 1.0);
}

// Replace Color  (Unity: Replace Color)
vec3 replaceColor(vec3 color, vec3 from, vec3 to, float range, float fuzziness) {
    float mask = colorMask(color, from, range, fuzziness);
    return mix(color, to, mask);
}

// ============================================================================
// Channel Operations  (Unity: Channel > Split, Combine, Swizzle)
// ============================================================================

// Split channels  (Unity: Split)
// Use .r / .g / .b / .a directly — GLSL swizzle is the idiomatic way.

// Combine channels  (Unity: Combine)
vec4 combineChannels(float r, float g, float b, float a) {
    return vec4(r, g, b, a);
}
vec3 combineChannels(float r, float g, float b) {
    return vec3(r, g, b);
}
vec2 combineChannels(float r, float g) {
    return vec2(r, g);
}

// Swizzle helper — GLSL has built-in .xyzw swizzle, use that directly.

// ============================================================================
// Blend Modes  (Unity: Artistic > Blend category)
// ============================================================================

// Blend Burn  (Unity: Blend > Burn)
vec3 blendBurn(vec3 base, vec3 blend, float opacity) {
    vec3 result = max(vec3(0.0), 1.0 - (1.0 - base) / max(blend, vec3(0.001)));
    return mix(base, result, opacity);
}

// Blend Darken  (Unity: Blend > Darken)
vec3 blendDarken(vec3 base, vec3 blend, float opacity) {
    return mix(base, min(base, blend), opacity);
}

// Blend Difference  (Unity: Blend > Difference)
vec3 blendDifference(vec3 base, vec3 blend, float opacity) {
    return mix(base, abs(base - blend), opacity);
}

// Blend Dodge  (Unity: Blend > Dodge)
vec3 blendDodge(vec3 base, vec3 blend, float opacity) {
    vec3 result = base / max(1.0 - blend, vec3(0.001));
    return mix(base, min(result, vec3(1.0)), opacity);
}

// Blend Divide  (Unity: Blend > Divide)
vec3 blendDivide(vec3 base, vec3 blend, float opacity) {
    return mix(base, base / max(blend, vec3(0.001)), opacity);
}

// Blend Exclusion  (Unity: Blend > Exclusion)
vec3 blendExclusion(vec3 base, vec3 blend, float opacity) {
    vec3 result = base + blend - 2.0 * base * blend;
    return mix(base, result, opacity);
}

// Blend Hard Light  (Unity: Blend > Hard Light)
vec3 blendHardLight(vec3 base, vec3 blend, float opacity) {
    vec3 result = mix(
        2.0 * base * blend,
        1.0 - 2.0 * (1.0 - base) * (1.0 - blend),
        step(0.5, blend)
    );
    return mix(base, result, opacity);
}

// Blend Hard Mix  (Unity: Blend > Hard Mix)
vec3 blendHardMix(vec3 base, vec3 blend, float opacity) {
    return mix(base, step(1.0 - base, blend), opacity);
}

// Blend Lighten  (Unity: Blend > Lighten)
vec3 blendLighten(vec3 base, vec3 blend, float opacity) {
    return mix(base, max(base, blend), opacity);
}

// Blend Linear Burn  (Unity: Blend > Linear Burn)
vec3 blendLinearBurn(vec3 base, vec3 blend, float opacity) {
    return mix(base, max(base + blend - 1.0, vec3(0.0)), opacity);
}

// Blend Linear Dodge  (Unity: Blend > Linear Dodge / Add)
vec3 blendLinearDodge(vec3 base, vec3 blend, float opacity) {
    return mix(base, min(base + blend, vec3(1.0)), opacity);
}

// Blend Linear Light  (Unity: Blend > Linear Light)
vec3 blendLinearLight(vec3 base, vec3 blend, float opacity) {
    vec3 result = clamp(base + 2.0 * blend - 1.0, vec3(0.0), vec3(1.0));
    return mix(base, result, opacity);
}

// Blend Multiply  (Unity: Blend > Multiply)
vec3 blendMultiply(vec3 base, vec3 blend, float opacity) {
    return mix(base, base * blend, opacity);
}

// Blend Negation  (Unity: Blend > Negation)
vec3 blendNegation(vec3 base, vec3 blend, float opacity) {
    return mix(base, 1.0 - abs(1.0 - base - blend), opacity);
}

// Blend Overlay  (Unity: Blend > Overlay)
vec3 blendOverlay(vec3 base, vec3 blend, float opacity) {
    vec3 result = mix(
        2.0 * base * blend,
        1.0 - 2.0 * (1.0 - base) * (1.0 - blend),
        step(0.5, base)
    );
    return mix(base, result, opacity);
}

// Blend Pin Light  (Unity: Blend > Pin Light)
vec3 blendPinLight(vec3 base, vec3 blend, float opacity) {
    vec3 check = step(0.5, blend);
    vec3 result = mix(min(base, 2.0 * blend), max(base, 2.0 * blend - 1.0), check);
    return mix(base, result, opacity);
}

// Blend Screen  (Unity: Blend > Screen)
vec3 blendScreen(vec3 base, vec3 blend, float opacity) {
    return mix(base, 1.0 - (1.0 - base) * (1.0 - blend), opacity);
}

// Blend Soft Light  (Unity: Blend > Soft Light)
vec3 blendSoftLight(vec3 base, vec3 blend, float opacity) {
    vec3 result = mix(
        2.0 * base * blend + base * base * (1.0 - 2.0 * blend),
        sqrt(base) * (2.0 * blend - 1.0) + 2.0 * base * (1.0 - blend),
        step(0.5, blend)
    );
    return mix(base, result, opacity);
}

// Blend Subtract  (Unity: Blend > Subtract)
vec3 blendSubtract(vec3 base, vec3 blend, float opacity) {
    return mix(base, max(base - blend, vec3(0.0)), opacity);
}

// Blend Vivid Light  (Unity: Blend > Vivid Light)
vec3 blendVividLight(vec3 base, vec3 blend, float opacity) {
    vec3 burn = max(vec3(0.0), 1.0 - (1.0 - base) / max(2.0 * blend, vec3(0.001)));
    vec3 dodge = base / max(2.0 * (1.0 - blend), vec3(0.001));
    vec3 result = mix(burn, min(dodge, vec3(1.0)), step(0.5, blend));
    return mix(base, result, opacity);
}

// Blend Overwrite  (Unity: Blend > Overwrite)
vec3 blendOverwrite(vec3 base, vec3 blend, float opacity) {
    return mix(base, blend, opacity);
}

// ============================================================================
// Tone Mapping
// ============================================================================

// Reinhard  (simple)
vec3 reinhardTonemap(vec3 color) {
    return color / (1.0 + color);
}

// Reinhard extended (with max white)
vec3 reinhardExtended(vec3 color, float maxWhite) {
    vec3 num = color * (1.0 + color / (maxWhite * maxWhite));
    return num / (1.0 + color);
}

// ACES approximation (Narkowicz 2015)
vec3 acesTonemap(vec3 x) {
    float a = 2.51;
    float b = 0.03;
    float c = 2.43;
    float d = 0.59;
    float e = 0.14;
    return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}

// Uncharted 2 / Filmic  (John Hable)
vec3 _filmicPartial(vec3 x) {
    float A = 0.15; float B = 0.50; float C = 0.10;
    float D = 0.20; float E = 0.02; float F = 0.30;
    return ((x * (A * x + C * B) + D * E) / (x * (A * x + B) + D * F)) - E / F;
}
vec3 filmicTonemap(vec3 color) {
    float W = 11.2; // Linear White
    vec3 curr = _filmicPartial(color * 2.0);
    vec3 whiteScale = vec3(1.0) / _filmicPartial(vec3(W));
    return curr * whiteScale;
}

// ============================================================================
// Utility
// ============================================================================

// Grayscale — desaturate to single channel  (Unity: implied by Saturation at 0)
float grayscale(vec3 color) {
    return dot(color, vec3(0.2126, 0.7152, 0.0722));
}

// Normal Unpack: use lib/texture_utils → unpackNormalRG(vec2 rg) instead.
