// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "parakeet-coreml-sidecar",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "parakeet-coreml-sidecar", targets: ["parakeet-coreml-sidecar"]),
    ],
    dependencies: [
        // CoreML + ANE Parakeet runtime (same package VoiceInk uses).
        // Pinned to a recent release that ships `AsrModels.v3`.
        .package(url: "https://github.com/FluidInference/FluidAudio.git", from: "0.5.0"),
        // Tiny async-first HTTP server. Pulls in swift-nio transitively.
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "parakeet-coreml-sidecar",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio"),
                .product(name: "Hummingbird", package: "hummingbird"),
            ]
        ),
    ]
)
