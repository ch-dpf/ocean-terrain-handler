param([string]$RunPrefix = 'canonicaledges')
$ErrorActionPreference = 'Stop'
foreach ($round in 1..3) {
    foreach ($sample in @('s85e80', 's5e130', 'n0e0')) {
        $branches = if ($round % 2) { @('main', 'current') } else { @('current', 'main') }
        foreach ($branch in $branches) {
            $imageName = if ($branch -eq 'main') { 'cesium-terrain-builder:local' } else { 'terrain-preprocess-dev' }
            docker run --rm --cpus 4 -e PREPROCESS_NATIVE_DIR=/data/preprocess_optimized_native -v //d/workspace/ocean-terrain-handler:/code:ro -v //d/workspace/ocean-terrain-handler-main:/baseline:ro -v ocean-terrain-handler_workspace_data:/data $imageName python3 /code/benchmarks/benchmark_tiling_production.py $branch "$RunPrefix$round" $sample
            if ($LASTEXITCODE -ne 0) { throw "Benchmark failed: $branch $round $sample" }
        }
    }
}
docker run --rm --cpus 4 -v //d/workspace/ocean-terrain-handler:/code:ro -v //d/workspace/ocean-terrain-handler/data/source:/source:ro -v ocean-terrain-handler_workspace_data:/data terrain-preprocess-dev python3 /code/benchmarks/evaluate_tiling_production.py "${RunPrefix}1"
if ($LASTEXITCODE -ne 0) { throw 'Accuracy evaluation failed' }

