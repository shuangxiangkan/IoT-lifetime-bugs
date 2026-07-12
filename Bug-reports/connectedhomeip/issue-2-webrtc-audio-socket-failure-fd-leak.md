# GitHub issue draft — project-chip/connectedhomeip bug report template

## Title

[BUG] `WebRTCClient::CreatePeerConnection()` does not unwind when creating the audio RTP socket fails

## Reproduction steps

This is an error-path cleanup issue in `src/controller/webrtc/WebRTCClient.cpp`, found by code inspection (static lifetime analysis). It triggers when the video RTP socket is created successfully but the audio RTP socket creation fails (e.g. the process hits its fd limit between the two calls):

1. Call `WebRTCClient::CreatePeerConnection()` (e.g. via the Python controller's `webrtc_client_create_peer_connection`).
2. Let the first `socket()` call succeed and the second one fail:

```cpp
// Create UDP sockets for RTP forwarding
mVideoRTPSocket = socket(AF_INET, SOCK_DGRAM, 0);
if (mVideoRTPSocket == -1)
{
    ChipLogError(Camera, "Failed to create RTP socket: %s", strerror(errno));
    return CHIP_ERROR_POSIX(errno);
}

mAudioRTPSocket = socket(AF_INET, SOCK_DGRAM, 0);
if (mAudioRTPSocket == -1)
{
    ChipLogError(Camera, "Failed to create RTP Audio socket: %s", strerror(errno));
    return CHIP_ERROR_POSIX(errno);
}
```

3. The audio-socket failure branch returns without closing the already-open video socket, even though the class has a cleanup helper for exactly this:

```cpp
void WebRTCClient::CloseRTPSocket()
{
    ChipLogProgress(Camera, "Closing RTP sockets");
    if (mVideoRTPSocket != -1)
    {
        close(mVideoRTPSocket);
        mVideoRTPSocket = -1;
    }

    if (mAudioRTPSocket != -1)
    {
        close(mAudioRTPSocket);
        mAudioRTPSocket = -1;
    }
}
```

4. The failure also leaves the object stuck: `mPeerConnection` was already created earlier in the function and is not torn down on this path, so retrying `CreatePeerConnection()` on the same object returns `CHIP_ERROR_ALREADY_INITIALIZED` at the top of the function. The caller is left holding an object that reported failed initialization, cannot be re-initialized, and keeps an open UDP socket (and a live `PeerConnection`) until it is destroyed or `Disconnect()` is called.

## Bug prevalence

Rare in practice — it requires `socket()` to fail for the audio socket after the video socket succeeded (typically fd exhaustion). But when it happens, the object is permanently unusable and holds an open fd for its remaining lifetime.

## GitHub hash of the SDK that was being used

236733d916fcb44665d747e250374c39affe0f9f (also verified present on current `master`)

## Platform

core

## Platform Version(s)

N/A

## Anything else?

Suggested fix: unwind on the `mAudioRTPSocket == -1` branch — call `CloseRTPSocket()` (which already handles the video socket and resets it to `-1`), and consider also tearing down `mPeerConnection` so the object returns to a state where `CreatePeerConnection()` can be retried.
