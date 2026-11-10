import { createThemeStore } from '@iris-ui-kit/theme'
import { lightTheme, darkTheme } from '@iris-ui-kit/tokens'

export const theme = createThemeStore({
  themes: {
    light: { ...lightTheme, colors: { ...lightTheme.colors, 'iris.primary': '#5b52d6', 'iris.primary.foreground': '#ffffff',
      'iris.background': '#ffffff', 'iris.surface': '#f5f6fa', 'iris.border': '#e5e7ef', 'iris.foreground': '#202332', 'iris.muted': '#6b7085' } },
    dark: { ...darkTheme, colors: { ...darkTheme.colors, 'iris.primary': '#ada5ff', 'iris.primary.foreground': '#1b1834',
      'iris.background': '#1b1e2a', 'iris.surface': '#141722', 'iris.border': '#34394e', 'iris.foreground': '#edf0fa', 'iris.muted': '#a2abc0' } },
  },
  default: 'light',
})
