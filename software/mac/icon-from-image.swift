// Cuts a macOS app icon out of a generated picture: finds the dark rounded square in the image, clips it with a
// rounded-rect mask (transparent outside) and scales it onto Apple's icon grid (824 px in a 1024 px canvas).
//   swift mac/icon-from-image.swift in.jpg out.png [corner-radius-fraction=0.25] [dark-threshold=80]
import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

let args = CommandLine.arguments
guard args.count >= 3 else { print("usage: icon-from-image.swift in out.png [radius-fraction] [dark-threshold]"); exit(2) }
let radiusFrac = args.count > 3 ? Double(args[3]) ?? 0.25 : 0.25
let dark = args.count > 4 ? Int(args[4]) ?? 80 : 80
guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { print("cannot read \(args[1])"); exit(1) }
let w = img.width, h = img.height
let cs = CGColorSpaceCreateDeviceRGB()
var data = [UInt8](repeating: 0, count: w * h * 4)
guard let ctx = CGContext(data: &data, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4, space: cs,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { exit(1) }
ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
var rows = [Int](repeating: 0, count: h), cols = [Int](repeating: 0, count: w)
for y in 0..<h {
    for x in 0..<w {
        let i = (y * w + x) * 4
        if Int(data[i]) < dark && Int(data[i + 1]) < dark && Int(data[i + 2]) < dark { rows[y] += 1; cols[x] += 1 }
    }
}
let thr = w / 25                                     // a row/column counts when 4% of it is dark: ignores shadow and noise
var minX = w, maxX = -1, minY = h, maxY = -1
for y in 0..<h where rows[y] > thr { minY = min(minY, y); maxY = max(maxY, y) }
for x in 0..<w where cols[x] > thr { minX = min(minX, x); maxX = max(maxX, x) }
guard maxX > minX, maxY > minY else { print("no dark shape found"); exit(1) }
let inset = 3
let crop = CGRect(x: minX + inset, y: minY + inset, width: maxX - minX + 1 - 2 * inset, height: maxY - minY + 1 - 2 * inset)
guard let cropped = img.cropping(to: crop) else { exit(1) }
let N = 1024, grid = 824.0
let scale = grid / Double(max(crop.width, crop.height))
let tw = crop.width * scale, th = crop.height * scale
let target = CGRect(x: (Double(N) - tw) / 2, y: (Double(N) - th) / 2, width: tw, height: th)
guard let out = CGContext(data: nil, width: N, height: N, bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { exit(1) }
out.clear(CGRect(x: 0, y: 0, width: N, height: N))
let radius = radiusFrac * min(tw, th)
out.addPath(CGPath(roundedRect: target, cornerWidth: radius, cornerHeight: radius, transform: nil))
out.clip()
out.interpolationQuality = .high
out.draw(cropped, in: target)
guard let result = out.makeImage(),
      let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: args[2]) as CFURL, UTType.png.identifier as CFString, 1, nil) else { exit(1) }
CGImageDestinationAddImage(dest, result, nil)
CGImageDestinationFinalize(dest)
print("shape \(minX),\(minY) \(Int(crop.width))x\(Int(crop.height)) -> \(args[2])")
