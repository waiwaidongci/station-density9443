import {runStates,reviewStates,purgeStates} from '../../constants/enums.js';import {esc} from '../../utils/format.js';
export function status(state){return '<span class="status '+esc(state)+'">'+esc(runStates[state]||reviewStates[state]||purgeStates[state]||state)+'</span>'}
