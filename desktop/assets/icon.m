// Build-time rendering of the existing GitBranch mark from frontend/src/components/Icons.tsx.
// Uses only macOS AppKit, clang, and iconutil. No external artwork or runtime dependencies.
#import <AppKit/AppKit.h>

static NSColor *color(unsigned value, CGFloat alpha) {
    return [NSColor colorWithSRGBRed:((value >> 16) & 255) / 255.0
                              green:((value >> 8) & 255) / 255.0
                               blue:(value & 255) / 255.0 alpha:alpha];
}

static NSPoint point(CGFloat x, CGFloat y) {
    return NSMakePoint(176 + x * 28, 1024 - (176 + y * 28));
}

static NSData *render(NSInteger size) {
    NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc]
        initWithBitmapDataPlanes:NULL pixelsWide:size pixelsHigh:size
        bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO
        colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:size * 4 bitsPerPixel:32];
    NSGraphicsContext *context = [NSGraphicsContext graphicsContextWithBitmapImageRep:bitmap];
    [NSGraphicsContext saveGraphicsState];
    [NSGraphicsContext setCurrentContext:context];
    CGContextScaleCTM(context.CGContext, size / 1024.0, size / 1024.0);
    CGContextSetShouldAntialias(context.CGContext, true);

    NSBezierPath *tile = [NSBezierPath bezierPathWithRoundedRect:NSMakeRect(72, 72, 880, 880)
                                                     xRadius:194 yRadius:194];
    [NSGraphicsContext saveGraphicsState];
    NSShadow *shadow = [NSShadow new];
    shadow.shadowColor = [[NSColor blackColor] colorWithAlphaComponent:0.22];
    shadow.shadowOffset = NSMakeSize(0, -12);
    shadow.shadowBlurRadius = 22;
    [shadow set];
    [color(0x16332D, 1) setFill];
    [tile fill];
    [NSGraphicsContext restoreGraphicsState];
    NSGradient *gradient = [[NSGradient alloc] initWithStartingColor:color(0x142D29, 1)
                                                       endingColor:color(0x2B4C40, 1)];
    [gradient drawInBezierPath:tile angle:90];
    [color(0xF3EEDD, 0.11) setStroke];
    tile.lineWidth = 2;
    [tile stroke];

    // SVG source: M6 3v18m0-7h5a7 7 0 0 0 7-7V3.
    NSBezierPath *mark = [NSBezierPath bezierPath];
    [mark moveToPoint:point(6, 3)];
    [mark lineToPoint:point(6, 21)];
    [mark moveToPoint:point(6, 14)];
    [mark lineToPoint:point(11, 14)];
    [mark curveToPoint:point(18, 7) controlPoint1:point(14.86599, 14)
         controlPoint2:point(18, 10.86599)];
    [mark lineToPoint:point(18, 3)];
    mark.lineWidth = 56;
    mark.lineCapStyle = NSLineCapStyleRound;
    mark.lineJoinStyle = NSLineJoinStyleRound;
    [color(0xF3EEDD, 1) setStroke];
    [mark stroke];
    [NSGraphicsContext restoreGraphicsState];
    return [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *directory = argc > 1 ? @(argv[1]) : @"desktop/build";
        NSURL *output = [NSURL fileURLWithPath:directory isDirectory:YES];
        NSURL *iconset = [output URLByAppendingPathComponent:@"AgentForge.iconset" isDirectory:YES];
        NSError *error = nil;
        if (![[NSFileManager defaultManager] createDirectoryAtURL:iconset
            withIntermediateDirectories:YES attributes:nil error:&error]) {
            fprintf(stderr, "无法创建图标目录：%s\n", error.localizedDescription.UTF8String);
            return 1;
        }
        for (NSNumber *size in @[@16, @32, @128, @256, @512]) {
            for (NSNumber *scale in @[@1, @2]) {
                NSString *filename = [NSString stringWithFormat:@"icon_%@x%@%@.png",
                    size, size, scale.integerValue == 2 ? @"@2x" : @""];
                NSData *png = render(size.integerValue * scale.integerValue);
                if (!png || ![png writeToURL:[iconset URLByAppendingPathComponent:filename]
                                    options:NSDataWritingAtomic error:&error]) {
                    fprintf(stderr, "无法写入图标图像\n");
                    return 1;
                }
            }
        }
        NSURL *destination = [output URLByAppendingPathComponent:@"AgentForge.icns"];
        NSTask *tool = [NSTask new];
        tool.executableURL = [NSURL fileURLWithPath:@"/usr/bin/iconutil"];
        tool.arguments = @[@"--convert", @"icns", @"--output", destination.path, iconset.path];
        if (![tool launchAndReturnError:&error]) {
            fprintf(stderr, "无法运行 macOS 图标工具\n");
            return 1;
        }
        [tool waitUntilExit];
        if (tool.terminationStatus != 0) return tool.terminationStatus;
        printf("%s\n", destination.path.UTF8String);
    }
    return 0;
}
