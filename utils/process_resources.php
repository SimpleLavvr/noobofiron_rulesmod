<?php

// Define the multiplier constant
define('MULTIPLIER', 12);

// Check command-line arguments
if ($argc < 2) {
    echo "Usage: php process_resources.php [-r] [-s] <path/*.txt>\n";
    echo "  -r  Recurse into directories\n";
    echo "  -s  Safe mode: only multiply values not divisible by " . MULTIPLIER . "\n";
    exit(1);
}

// Parse flags
$isRecursive = false;
$safeMode = false;
$patterns = [];

for ($i = 1; $i < $argc; $i++) {
    if ($argv[$i] === '-r') {
        $isRecursive = true;
    } elseif ($argv[$i] === '-s') {
        $safeMode = true;
    } else {
        $patterns[] = $argv[$i];
    }
}

// Function to decide whether to multiply a value
function shouldMultiply($value) {
    global $safeMode;
    if (!$safeMode) return true;
    return ($value % MULTIPLIER) !== 0;
}

// Function to process each file
function processFile($file) {
    $content = file_get_contents($file);
    $changed = false;

    // Process `add_resource` blocks
    $patternAddResource = '/(add_resource = \{.*?\})/s';
    $callbackAddResource = function($matches) use (&$changed) {
        $block = $matches[1];

        if (strpos($block, 'type = any') === false) {
            $block = preg_replace_callback(
                '/(\s*amount\s*=\s*)(\d+)/',
                function($amountMatches) use (&$changed) {
                    $val = (int)$amountMatches[2];
                    if (shouldMultiply($val)) {
                        $changed = true;
                        return $amountMatches[1] . ($val * MULTIPLIER);
                    }
                    return $amountMatches[0];
                },
                $block
            );
        }

        return $block;
    };
    $content = preg_replace_callback($patternAddResource, $callbackAddResource, $content);

    // Process `resources = { ... }` blocks
    $patternResources = '/resources\s*=\s*\{([^}]*)\}/s';
    $callbackResources = function($matches) use (&$changed) {
        $resourcesBlock = $matches[1];

        $resourcesBlock = preg_replace_callback(
            '/(\s*)([a-zA-Z_]+)\s*=\s*(\d+)(\s*#.*)?/',
            function($resourceMatches) use (&$changed) {
                $resourceName = $resourceMatches[2];
                $resourceValue = (int)$resourceMatches[3];
                $comment = $resourceMatches[4] ?? '';

                if ($resourceName === 'any') {
                    return $resourceMatches[0];
                }
                if (shouldMultiply($resourceValue)) {
                    $changed = true;
                    return $resourceMatches[1] . $resourceName . ' = ' . ($resourceValue * MULTIPLIER) . $comment;
                }
                return $resourceMatches[0];
            },
            $resourcesBlock
        );

        return 'resources={' . $resourcesBlock . '}';
    };
    $content = preg_replace_callback($patternResources, $callbackResources, $content);

    if ($changed) {
        file_put_contents($file, $content);
        echo "Modified: $file\n";
    } else {
        echo "Unchanged: $file\n";
    }
}

// Function to process files in a directory recursively
function processDirectory($dir) {
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($dir));
    foreach ($iterator as $fileinfo) {
        if ($fileinfo->isFile() && $fileinfo->getExtension() === 'txt') {
            processFile($fileinfo->getPathname());
        }
    }
}

foreach ($patterns as $pattern) {
    if ($isRecursive) {
        processDirectory($pattern);
    } else {
        foreach (glob($pattern) as $file) {
            processFile($file);
        }
    }
}
?>
