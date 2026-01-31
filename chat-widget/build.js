#!/usr/bin/env node

/**
 * Build script for DDP Chat Widget
 *
 * Combines CSS and JavaScript into a single minified file.
 *
 * Usage:
 *   node build.js         # Build once
 *   node build.js --watch # Watch for changes
 */

const fs = require('fs');
const path = require('path');

// Check if terser is available
let terser;
try {
    terser = require('terser');
} catch (e) {
    console.log('Note: terser not installed, output will not be minified.');
    console.log('Run "npm install" to enable minification.\n');
}

const SRC_DIR = path.join(__dirname, 'src');
const DIST_DIR = path.join(__dirname, 'dist');
const OUTPUT_FILE = path.join(DIST_DIR, 'ddp-chat.min.js');

// Files to combine in order (dependencies first)
const JS_FILES = [
    'websocket.js',
    'chat.js',
    'ui.js',
    'widget.js'
];

const CSS_FILE = 'styles.css';

/**
 * Read and combine all source files.
 */
function buildWidget() {
    console.log('Building DDP Chat Widget...\n');

    // Ensure dist directory exists
    if (!fs.existsSync(DIST_DIR)) {
        fs.mkdirSync(DIST_DIR, { recursive: true });
    }

    // Read CSS
    const cssPath = path.join(SRC_DIR, CSS_FILE);
    let css = '';
    if (fs.existsSync(cssPath)) {
        css = fs.readFileSync(cssPath, 'utf8');
        console.log(`  Read: ${CSS_FILE}`);
    } else {
        console.error(`  ERROR: ${CSS_FILE} not found`);
        process.exit(1);
    }

    // Minify CSS (simple minification)
    css = css
        .replace(/\/\*[\s\S]*?\*\//g, '')  // Remove comments
        .replace(/\s+/g, ' ')               // Collapse whitespace
        .replace(/\s*([{}:;,>+~])\s*/g, '$1') // Remove space around special chars
        .replace(/;}/g, '}')                // Remove trailing semicolons
        .trim();

    // Read and combine JS files
    let combinedJS = '';
    for (const file of JS_FILES) {
        const filePath = path.join(SRC_DIR, file);
        if (fs.existsSync(filePath)) {
            let content = fs.readFileSync(filePath, 'utf8');

            // For widget.js, inject the CSS
            if (file === 'widget.js') {
                // Escape CSS for JavaScript string
                const escapedCSS = css
                    .replace(/\\/g, '\\\\')
                    .replace(/'/g, "\\'");
                content = content.replace(
                    "var WIDGET_CSS = '/* CSS_PLACEHOLDER */';",
                    `var WIDGET_CSS = '${escapedCSS}';`
                );
            }

            combinedJS += content + '\n';
            console.log(`  Read: ${file}`);
        } else {
            console.error(`  ERROR: ${file} not found`);
            process.exit(1);
        }
    }

    // Minify if terser is available
    if (terser) {
        console.log('\n  Minifying...');
        terser.minify(combinedJS, {
            compress: {
                drop_console: false, // Keep console.log for debugging
                passes: 2
            },
            mangle: {
                reserved: ['DDPChatWidget', 'DDPChatConfig']
            },
            format: {
                comments: false
            }
        }).then(result => {
            if (result.error) {
                console.error('  Minification error:', result.error);
                // Fall back to unminified
                writeOutput(combinedJS);
            } else {
                writeOutput(result.code);
            }
        });
    } else {
        writeOutput(combinedJS);
    }
}

/**
 * Write output file.
 */
function writeOutput(content) {
    // Add banner
    const banner = `/**
 * DDP Chat Widget v1.0.0
 * Digital Democracy Project - VoteBot Embeddable Widget
 * https://digitaldemocracyproject.org
 *
 * Built: ${new Date().toISOString()}
 */
`;

    fs.writeFileSync(OUTPUT_FILE, banner + content);

    const stats = fs.statSync(OUTPUT_FILE);
    const sizeKB = (stats.size / 1024).toFixed(2);

    console.log(`\n  Output: ${OUTPUT_FILE}`);
    console.log(`  Size: ${sizeKB} KB`);
    console.log('\nBuild complete!');
}

/**
 * Watch mode for development.
 */
function watchMode() {
    console.log('Watching for changes...\n');

    const filesToWatch = [
        path.join(SRC_DIR, CSS_FILE),
        ...JS_FILES.map(f => path.join(SRC_DIR, f))
    ];

    let debounceTimer;
    filesToWatch.forEach(file => {
        if (fs.existsSync(file)) {
            fs.watch(file, () => {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    console.log(`\nChange detected: ${path.basename(file)}`);
                    buildWidget();
                }, 100);
            });
        }
    });
}

// Main
buildWidget();

if (process.argv.includes('--watch')) {
    watchMode();
}
