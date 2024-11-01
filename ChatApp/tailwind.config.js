/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './templates/**/*.html', 
    './static/js/**/*.js', 
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        indigo: { 50: '#EEF2FF', 500: '#6366F1', 600: '#4F46E5', 700: '#4338CA' },
        purple: { 50: '#F5F3FF', 500: '#8B5CF6', 600: '#7C3AED' },
        pink: { 50: '#FDF2F8' },
        gray: { 50: '#F9FAFB', 100: '#F3F4F6', 200: '#E5E7EB', 300: '#D1D5DB', 500: '#6B7280', 600: '#4B5563', 700: '#374151', 800: '#1F2937', 900: '#111827' }
      },
      height: { screen: '100vh', '[100dvh]': '100dvh' },
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/aspect-ratio'),
    require('@tailwindcss/line-clamp'),
  ],
}
