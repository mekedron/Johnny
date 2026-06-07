// CoreML + Apple Neural Engine Parakeet sidecar for the Johnny api container.
//
// Runs natively on the macOS host (NOT inside Docker) so it can call into
// CoreML and the Apple Neural Engine. The api container POSTs raw 16 kHz
// mono S16LE PCM bytes here; we transcribe via FluidAudio (the same Swift
// package VoiceInk uses) and return the text. ANE acceleration matches
// VoiceInk's ~150 ms / 5 s of audio.
//
// Wire protocol (must match `app.providers.parakeet_stt._transcribe_via_sidecar`):
//   POST /transcribe   — raw 16 kHz mono S16LE PCM bytes in,
//                        `{"text": "...", "confidence": null}` out.
//   GET  /health       — `{"ready": true, "model_id": "...", "backend": "coreml-ane"}`
//
// Env vars:
//   PARAKEET_COREML_HOST   bind host (default 127.0.0.1; use 0.0.0.0 to
//                          accept remote connections from a non-Desktop
//                          Docker setup).
//   PARAKEET_COREML_PORT   bind port (default 8766).

import Foundation
import FluidAudio
import Hummingbird
import HTTPTypes
import NIOCore

// Shared state guarded by an actor so request handlers can safely access
// the AsrManager from any worker. The manager is the expensive thing to
// construct (downloads + compiles CoreML models); we load it once on
// startup and reuse across requests.
actor ParakeetState {
    private(set) var asrManager: AsrManager?
    private(set) var ready: Bool = false
    private(set) var loadError: String?
    let modelId: String = "parakeet-tdt-0.6b-v3"

    func load() async {
        let start = Date()
        do {
            let models = try await AsrModels.downloadAndLoad(
                configuration: nil,
                version: .v3
            )
            let manager = AsrManager(config: .default)
            try await manager.loadModels(models)
            self.asrManager = manager
            self.ready = true
            let elapsedMs = Int(Date().timeIntervalSince(start) * 1000)
            print("[parakeet-coreml-sidecar] model \(modelId) loaded in \(elapsedMs) ms")
        } catch {
            self.loadError = "\(error)"
            print("[parakeet-coreml-sidecar] model load FAILED: \(error)")
        }
    }
}

// JSON payload types.
struct HealthPayload: Codable {
    let ready: Bool
    let model_id: String  // snake_case to match the Python sidecar wire format
    let backend: String
    let error: String?
}

struct TranscriptPayload: Codable {
    let text: String
    let confidence: Double?
}

struct ErrorPayload: Codable {
    let error: String
}

func envHost() -> String {
    ProcessInfo.processInfo.environment["PARAKEET_COREML_HOST"] ?? "127.0.0.1"
}

func envPort() -> Int {
    if let raw = ProcessInfo.processInfo.environment["PARAKEET_COREML_PORT"],
       let parsed = Int(raw) {
        return parsed
    }
    return 8766
}

// 16 kHz mono S16LE PCM → Float32 array in [-1, 1]. Matches VoiceInk's
// exact normalisation (divide by 32767, clamp to ±1) so the model sees
// the same range it was trained on.
func pcmS16leToFloats(_ data: Data) -> [Float] {
    let count = data.count / 2
    var out = [Float](repeating: 0, count: count)
    data.withUnsafeBytes { rawBufferPointer in
        let int16Pointer = rawBufferPointer.bindMemory(to: Int16.self)
        for i in 0..<count {
            let s = Int16(littleEndian: int16Pointer[i])
            let f = Float(s) / 32767.0
            out[i] = max(-1.0, min(1.0, f))
        }
    }
    return out
}

// Pad with up to 1 s of silence so short utterances pick up the final
// punctuation at the sequence boundary — same idiom as VoiceInk's
// `FluidAudioTranscriptionService.transcribe`.
func withTrailingSilence(_ samples: [Float]) -> [Float] {
    let trailingSilenceSamples = 16_000
    let maxSingleChunkSamples = 240_000
    if samples.count + trailingSilenceSamples <= maxSingleChunkSamples {
        return samples + [Float](repeating: 0, count: trailingSilenceSamples)
    }
    return samples
}

// JSON helper: serialise an Encodable into a Hummingbird Response with
// `Content-Type: application/json`.
func jsonResponse<T: Encodable>(status: HTTPResponse.Status, payload: T) throws -> Response {
    let data = try JSONEncoder().encode(payload)
    var byteBuffer = ByteBufferAllocator().buffer(capacity: data.count)
    byteBuffer.writeBytes(data)
    return Response(
        status: status,
        headers: [.contentType: "application/json"],
        body: ResponseBody(byteBuffer: byteBuffer)
    )
}

@main
struct ParakeetCoreMLSidecar {
    static func main() async throws {
        let state = ParakeetState()
        // Kick off the model load before binding the port so /health can
        // report progress immediately. The first /transcribe will wait
        // on this if it arrives during the cold load.
        Task { await state.load() }

        print("[parakeet-coreml-sidecar] starting; model loading in background...")

        let router = Router()

        router.get("/health") { _, _ -> Response in
            let payload = HealthPayload(
                ready: await state.ready,
                model_id: await state.modelId,
                backend: "coreml-ane",
                error: await state.loadError
            )
            return try jsonResponse(status: .ok, payload: payload)
        }

        router.post("/transcribe") { request, _ -> Response in
            // Wait up to 45 s for the cold load if /transcribe lands
            // during it. The Python adapter's timeout is 60 s so this
            // leaves headroom for the actual forward pass.
            let waitDeadline = Date().addingTimeInterval(45)
            while !(await state.ready) && Date() < waitDeadline {
                if let err = await state.loadError {
                    return try jsonResponse(
                        status: .serviceUnavailable,
                        payload: ErrorPayload(error: err)
                    )
                }
                try await Task.sleep(nanoseconds: 100_000_000)  // 100 ms
            }
            guard await state.ready, let manager = await state.asrManager else {
                return try jsonResponse(
                    status: .serviceUnavailable,
                    payload: ErrorPayload(error: "model is still loading; check /health")
                )
            }

            // Read up to 10 MiB of PCM (~5 min of 16 kHz mono S16LE).
            let bodyBuffer = try await request.body.collect(upTo: 10 * 1024 * 1024)
            let pcm = Data(buffer: bodyBuffer)
            if pcm.isEmpty {
                return try jsonResponse(
                    status: .ok,
                    payload: TranscriptPayload(text: "", confidence: nil)
                )
            }
            if pcm.count % 2 != 0 {
                return try jsonResponse(
                    status: .badRequest,
                    payload: ErrorPayload(
                        error: "PCM body not aligned to 2-byte S16 samples"
                    )
                )
            }

            let samples = withTrailingSilence(pcmS16leToFloats(pcm))
            let start = Date()
            do {
                var decoderState = TdtDecoderState.make(
                    decoderLayers: await manager.decoderLayerCount
                )
                let result = try await manager.transcribe(
                    samples, decoderState: &decoderState
                )
                let elapsedMs = Int(Date().timeIntervalSince(start) * 1000)
                let audioMs = pcm.count * 1000 / (16_000 * 2)
                let text = result.text.trimmingCharacters(in: .whitespacesAndNewlines)
                print("[parakeet-coreml-sidecar] transcribe audio_ms=\(audioMs) forward_ms=\(elapsedMs) text_chars=\(text.count)")
                return try jsonResponse(
                    status: .ok,
                    payload: TranscriptPayload(text: text, confidence: nil)
                )
            } catch {
                return try jsonResponse(
                    status: .internalServerError,
                    payload: ErrorPayload(error: "\(error)")
                )
            }
        }

        let app = Application(
            router: router,
            configuration: .init(
                address: .hostname(envHost(), port: envPort()),
                serverName: "parakeet-coreml-sidecar"
            )
        )
        try await app.runService()
    }
}
