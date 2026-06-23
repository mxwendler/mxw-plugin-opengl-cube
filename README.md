# mxw-plugin-opengl-cube

An MXWendler StageDesigner **media plugin** that renders a spinning, colour-shaded
OpenGL cube into a media surface, using [ModernGL](https://github.com/moderngl/moderngl).

It is the OpenGL counterpart to the web-page media plugin and serves as a worked
example of the MXWendler Python media-plugin API.

## Usage

In MXWendler, create the media with the URI:

```
generative://cube_spin_opengl
```

(or pick **Spinning Cube** from the media create dropdown).

## How it works

MXWendler's media-plugin host calls three entry points in `mxw_main.py`:

| Callback | Returns | Purpose |
|----------|---------|---------|
| `onOpen(uri)` | `(width, height, length, fps, has_alpha)` | report the surface format |
| `onRenderFrame(frame)` | `H*W*4` uint8 buffer, **BGRA** byte order | produce one frame |
| `onClose()` | – | release resources |
| `onSizeChange(w, h)` | – | host changed the render size; rebuild buffers |
| `onSetSpeed(speed)` | – | clip playback speed changed; store per instance |

Per-instance state is keyed by the integer `media_id`, which the host sets on the
module before each call. The host pushes the clip playback speed through
`onSetSpeed(speed)`, which the cube stores per instance and uses to scale its
rotation, so `0` freezes the spin and negative values spin it backwards.

## Requirements

```
pip install moderngl numpy
```

## License

MIT
