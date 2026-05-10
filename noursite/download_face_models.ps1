# Telecharge les modeles face-api.js dans api/face_models/
$base = "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights"
$dir  = "$PSScriptRoot\api\face_models"

New-Item -ItemType Directory -Force $dir | Out-Null

$files = @(
    "tiny_face_detector_model-weights_manifest.json",
    "tiny_face_detector_model-shard1",
    "face_landmark_68_tiny_model-weights_manifest.json",
    "face_landmark_68_tiny_model-shard1",
    "face_recognition_model-weights_manifest.json",
    "face_recognition_model-shard1",
    "face_recognition_model-shard2"
)

foreach ($f in $files) {
    Invoke-WebRequest -Uri "$base/$f" -OutFile "$dir\$f"
    Write-Host "OK $f"
}

Write-Host "`nModeles telecharges dans $dir"
