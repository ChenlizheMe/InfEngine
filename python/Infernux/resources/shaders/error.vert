#version 450

ShaderInfo {
    Name "Error"
    Hidden On
}

// The error material uses the canonical mesh vertex path. Keeping it in the
// linked shader system guarantees that every required material pass is built
// from the same interface contract.
