#version 450

@shader_id: error
@hidden

// The error material uses the canonical mesh vertex path. Keeping it in the
// linked shader system guarantees that every required material pass is built
// from the same interface contract.
