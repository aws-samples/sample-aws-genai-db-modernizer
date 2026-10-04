// Jest (jsdom) setup, loaded automatically by react-scripts before each test file.
//
// react-router v7 uses TextEncoder/TextDecoder at module load. jsdom does not
// provide them as globals, so take Node's implementations.
import { TextEncoder, TextDecoder } from 'util';

if (typeof global.TextEncoder === 'undefined') global.TextEncoder = TextEncoder;
if (typeof global.TextDecoder === 'undefined') global.TextDecoder = TextDecoder;
