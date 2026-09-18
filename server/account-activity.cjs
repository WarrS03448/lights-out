'use strict';
// The match service runs in one process. Ownership changes exclude live request
// admission and match formation; readers queued after a writer cannot overtake it.
function create() {
  let readers=0, writing=false;
  const waiting=[];
  function pump() {
    if(writing || !waiting.length)return;
    if(waiting[0].write) {
      if(readers)return;
      writing=true;waiting.shift().start(()=>{writing=false;pump();});return;
    }
    while(waiting.length&&!waiting[0].write) {
      readers++;waiting.shift().start(()=>{readers--;pump();});
    }
  }
  function run(write,work) {
    return new Promise((resolve,reject)=>{
      waiting.push({write,start:release=>Promise.resolve().then(work).then(resolve,reject).finally(release)});
      pump();
    });
  }
  return {read:work=>run(false,work),write:work=>run(true,work),get writing(){return writing;},
    get blocksFormation(){return writing || waiting.some(item=>item.write);}};
}
module.exports={create};
