# Local RapidOCR Design

## Goal

Replace cloud-based visual understanding in both image recognition and keyword retort flows with one shared, local OCR implementation. The bot will extract Simplified Chinese and English text only. It will no longer describe people, objects, expressions, or scenes.

The implementation must require no external AI account, API key, or paid model service.

## Selected Approach

Use RapidOCR with the CPU ONNX Runtime backend.

This approach fits the production host's two CPU cores, approximately 2 GiB of RAM, and lack of a GPU better than the full PaddleOCR runtime. It also provides stronger Chinese screenshot and meme-text recognition than a Tesseract-based design while remaining fully local.

## Architecture

Add a shared in-process OCR service with one clear responsibility: accept image bytes or a local image path and return normalized OCR text.

The service will:

- lazily initialize one RapidOCR engine and reuse it for the lifetime of the bot process;
- execute inference in a worker thread so OCR does not block the asynchronous QQ event loop;
- normalize detected lines into reading order;
- discard empty results and detections below the configured confidence threshold;
- cache results for a short period by a stable image identifier so the same image is not inferred twice;
- expose a small interface that is independent of RapidOCR result-object internals.

Both existing consumers will depend on this service:

- Image recognition replies with the extracted text.
- Keyword retort evaluates its configured keywords against the same extracted text.

The two consumers must not create separate OCR engines or make cloud vision requests.

## Data Flow

1. The bot receives a QQ image event and downloads the image using the existing media-download path.
2. Input validation rejects oversized or unsupported image data before inference.
3. The shared OCR service checks its short-lived cache.
4. On a cache miss, RapidOCR runs through the CPU ONNX Runtime backend in a worker thread with a bounded timeout.
5. The service filters low-confidence entries, preserves reading order, joins the recognized lines, and caches the normalized text.
6. Image recognition returns the normalized text to the user.
7. Keyword retort receives the same normalized text and runs its existing keyword matching and response behavior.

Only OCR text and a short-lived image identifier are cached. User images are not retained by the OCR feature beyond the existing temporary-download lifecycle.

## Configuration

Add local OCR settings for:

- enabling or disabling OCR;
- confidence threshold;
- inference timeout;
- maximum accepted compressed image size and decoded pixel count;
- cache lifetime and cache-size bound.

Defaults will be conservative for the production server. Existing cloud-vision environment variables will no longer be consulted by image recognition or keyword retort. Unrelated text-generation APIs remain unchanged.

## Error Handling and Resource Safety

- Image download failure, invalid image data, timeout, empty OCR output, and RapidOCR errors are handled without crashing the event loop.
- Automatic keyword retort silently skips when OCR is unavailable or produces no usable text.
- A direct image-recognition request returns a short, user-facing failure or no-text message.
- OCR inference concurrency is bounded to prevent multiple images from exhausting CPU or memory.
- If engine initialization fails, OCR is marked unavailable for subsequent calls with rate-limited logging; the rest of the bot continues running.
- The implementation does not fall back to any cloud or paid visual API.

## Testing

Unit tests will cover:

- Simplified Chinese and English line extraction;
- reading-order normalization;
- low-confidence and empty-result filtering;
- download, initialization, inference, and timeout failures;
- cache hits and expiration;
- concurrent calls sharing a single engine initialization;
- image-recognition responses for success, no text, and failure;
- keyword-retort matching from OCR text and silent skipping on OCR failure.

Integration tests will verify that both consumers use the shared OCR service and that neither issues a cloud vision request.

Production verification will:

1. Install pinned RapidOCR and CPU ONNX Runtime dependencies.
2. Run the RapidOCR installation check and a local Chinese/English fixture.
3. Run the complete automated test suite before deployment.
4. Restart the bot and confirm its service and OneBot connection remain healthy.
5. Verify through QQ with a text screenshot, a text-bearing meme, and an image with no text.
6. Observe process memory and logs during repeated OCR requests.

## Rollback

Rollback restores the previous application revision and dependency set. Cloud vision configuration may remain stored for unrelated historical compatibility, but this feature will not automatically reactivate or call it. If OCR is disabled or unavailable, image OCR and OCR-driven keyword retort stop while the rest of the bot remains operational.

## Acceptance Criteria

- Image recognition and keyword retort both use one local RapidOCR engine and one cached OCR result per image.
- Chinese and English text can be extracted from representative QQ screenshots and text-bearing memes.
- Neither flow requires or sends data to an external AI API.
- OCR failure never stops ordinary bot message handling.
- The production bot stays within safe memory limits under bounded OCR concurrency.
- Automated tests and live QQ verification pass.
