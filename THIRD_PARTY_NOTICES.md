# Third-party notices

## Cesium Terrain Builder

The native terrain meshing and quantized-mesh encoding code in
`app/services/ctb/native/` is adapted from
[`ch-dpf/cesium-terrain-builder`](https://github.com/ch-dpf/cesium-terrain-builder/tree/master-quantized-mesh-adaptation),
which derives from Cesium Terrain Builder.
The imported behavior is pinned to commit
`676719d22622c6ef754e2a348a14459df4ff2db6`.

Copyright 2014 GeoData <geodata@soton.ac.uk>

Licensed under the Apache License, Version 2.0. You may obtain a copy at
<https://www.apache.org/licenses/LICENSE-2.0>.

The adaptation preserves CTB's constants and compatibility behavior where
documented in source comments and tests.

# GDAL quadrant interpolation

The quadrant search in `app/services/ctb/native/fill_nodata.hpp` is adapted
from GDAL 3.12.4 `alg/rasterfill.cpp` (https://github.com/OSGeo/gdal).

Copyright (c) 2008, Frank Warmerdam
Copyright (c) 2009-2013, Even Rouault
Copyright (c) 2015, Sean Gillies

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
