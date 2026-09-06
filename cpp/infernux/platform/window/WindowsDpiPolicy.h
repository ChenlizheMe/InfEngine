#pragma once

namespace infernux
{

/// Configure the Windows process for Per-Monitor V2 before SDL video starts.
void ConfigureRequiredWindowsDpiPolicy();

/// Verify that SDL established the required Windows Per-Monitor V2 context.
void VerifyRequiredWindowsDpiPolicy();

} // namespace infernux
