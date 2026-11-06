package __NAMESPACE__.ui.kpm

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import top.yukonga.miuix.kmp.theme.Colors
import top.yukonga.miuix.kmp.theme.MiuixTheme
__THEME_IMPORTS__

/** Use the already resolved Miuix palette, including forced dark and Monet modes. */
internal fun kpmMaterialColors(native: Colors): ColorScheme = ColorScheme(
    primary = native.primary,
    onPrimary = native.onPrimary,
    primaryContainer = native.primaryContainer,
    onPrimaryContainer = native.onPrimaryContainer,
    inversePrimary = native.primaryVariant,
    secondary = native.secondary,
    onSecondary = native.onSecondary,
    secondaryContainer = native.secondaryContainer,
    onSecondaryContainer = native.onSecondaryContainer,
    tertiary = native.primaryVariant,
    onTertiary = native.onPrimary,
    tertiaryContainer = native.tertiaryContainer,
    onTertiaryContainer = native.onTertiaryContainer,
    background = native.background,
    onBackground = native.onBackground,
    surface = native.surface,
    onSurface = native.onSurface,
    surfaceVariant = native.surfaceVariant,
    onSurfaceVariant = native.onSurfaceVariantSummary,
    surfaceTint = native.surface,
    inverseSurface = native.onSurface,
    inverseOnSurface = native.surface,
    error = native.error,
    onError = native.onError,
    errorContainer = native.errorContainer,
    onErrorContainer = native.onErrorContainer,
    outline = native.outline,
    outlineVariant = native.dividerLine,
    scrim = native.windowDimming,
    surfaceBright = native.surfaceContainer,
    surfaceDim = native.surface,
    surfaceContainerLowest = native.background,
    surfaceContainerLow = native.surfaceContainer,
    surfaceContainer = native.surfaceContainer,
    surfaceContainerHigh = native.surfaceContainerHigh,
    surfaceContainerHighest = native.surfaceContainerHighest,
    primaryFixed = native.primaryContainer,
    primaryFixedDim = native.primaryVariant,
    onPrimaryFixed = native.onPrimaryContainer,
    onPrimaryFixedVariant = native.onPrimaryVariant,
    secondaryFixed = native.secondaryContainer,
    secondaryFixedDim = native.secondaryVariant,
    onSecondaryFixed = native.onSecondaryContainer,
    onSecondaryFixedVariant = native.onSecondaryVariant,
    tertiaryFixed = native.tertiaryContainer,
    tertiaryFixedDim = native.tertiaryContainerVariant,
    onTertiaryFixed = native.onTertiaryContainer,
    onTertiaryFixedVariant = native.onTertiaryContainer,
)

@Composable
internal fun GkiKpmTheme(content: @Composable () -> Unit) {
    if (__USE_MIUIX__) {
        // Do not remember a seed/default scheme: native colors can change in place.
        MaterialTheme(colorScheme = kpmMaterialColors(MiuixTheme.colorScheme), content = content)
    } else {
        content()
    }
}
