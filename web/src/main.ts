import { mount } from 'svelte'
import App from './App.svelte'
import './app.css'
import './tasks.css'

const target = document.getElementById('app')
if (!target) throw Error('ABR application mount is missing')
mount(App, { target })
