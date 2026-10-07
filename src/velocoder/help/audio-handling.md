**Choose how VeloCoder handles your video's audio track.**

![The Audio tab, showing Track, Handling, Channels, and AAC Bitrate](audio-tab.png)

**Automatic** keeps the original audio track byte-for-byte whenever it's already a compatible format (AAC, AC-3, E-AC-3) -- a fast copy with zero quality loss. It only re-encodes to AAC when the source track isn't already one of those.

**Convert to AAC** always re-encodes to AAC at the bitrate you've set, even if the source was already compatible. Use this only if you specifically need a fresh AAC stream regardless.

> **Recommended**
> Automatic -- it only re-encodes when it actually has to.
