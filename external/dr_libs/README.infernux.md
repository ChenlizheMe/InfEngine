# dr_libs

The bundled `dr_mp3.h` and `dr_flac.h` files come from
https://github.com/mackron/dr_libs at commit
`50bb723e6a459dbb781e26cefee4fd9ca6714d6a`.

Only the MP3 and FLAC decoders are included. Their implementation is compiled
once by `AudioDecoders.cpp`; engine code includes the declaration-only form.
The upstream files are offered under either public domain or MIT-0 terms, as
documented in each header.
