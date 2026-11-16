import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Analytics } from '@vercel/analytics/react'
import { SpeedInsights } from '@vercel/speed-insights/react'
import App from './App.tsx'
import { loadUmami } from './analytics'
import './index.css'

loadUmami()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
    {/* Vercel serves these scripts from /_vercel/*, which exists only there;
        in dev the components would pull a debug script from a third origin. */}
    {import.meta.env.PROD ? (
      <>
        <Analytics />
        <SpeedInsights />
      </>
    ) : null}
  </StrictMode>,
)
