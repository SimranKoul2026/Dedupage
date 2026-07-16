/*
 * page_dedup_vfs.c - Phase 3: the page-dedup SQLite VFS shim in C.
 *
 * A shim sqlite3_vfs that wraps the default (unix) VFS. Every write to the MAIN
 * DB file records a dirty byte-range. dedup_snapshot() reads the dirty pages,
 * SHA-256's each, and writes any content-new page to store/objects/<hex> (raw).
 * This is the production-representative version of shim/dedup_vfs.py.
 *
 * Cross-validation: run with the SAME workload as driver.py (DELETE journal),
 * and the set of object hashes produced here must equal the Python run's.
 *
 * Build (Mac):     cc -O2 page_dedup_vfs.c -lsqlite3 -o dedupage
 * Build (Android): $NDK/.../aarch64-linux-android<API>-clang -O2 page_dedup_vfs.c \
 *                    sqlite3.c -o dedupage   (bundle the amalgamation for NDK)
 */
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <sys/stat.h>
#include <time.h>

/* ----------------------------- SHA-256 (public domain, compact) ---------- */
typedef struct { uint32_t s[8]; uint64_t n; uint8_t buf[64]; size_t c; } SHA256;
static uint32_t rotr(uint32_t x,int n){return (x>>n)|(x<<(32-n));}
static void sha256_init(SHA256*h){
  static const uint32_t iv[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
    0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
  memcpy(h->s,iv,sizeof iv); h->n=0; h->c=0;
}
static void sha256_block(SHA256*h,const uint8_t*p){
  static const uint32_t k[64]={
   0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
   0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
   0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
   0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
   0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
   0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
   0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
   0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
  uint32_t w[64],a,b,c,d,e,f,g,hh,t1,t2; int i;
  for(i=0;i<16;i++) w[i]=(p[i*4]<<24)|(p[i*4+1]<<16)|(p[i*4+2]<<8)|p[i*4+3];
  for(i=16;i<64;i++){
    uint32_t s0=rotr(w[i-15],7)^rotr(w[i-15],18)^(w[i-15]>>3);
    uint32_t s1=rotr(w[i-2],17)^rotr(w[i-2],19)^(w[i-2]>>10);
    w[i]=w[i-16]+s0+w[i-7]+s1;
  }
  a=h->s[0];b=h->s[1];c=h->s[2];d=h->s[3];e=h->s[4];f=h->s[5];g=h->s[6];hh=h->s[7];
  for(i=0;i<64;i++){
    uint32_t S1=rotr(e,6)^rotr(e,11)^rotr(e,25), ch=(e&f)^(~e&g);
    t1=hh+S1+ch+k[i]+w[i];
    uint32_t S0=rotr(a,2)^rotr(a,13)^rotr(a,22), mj=(a&b)^(a&c)^(b&c);
    t2=S0+mj; hh=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
  }
  h->s[0]+=a;h->s[1]+=b;h->s[2]+=c;h->s[3]+=d;h->s[4]+=e;h->s[5]+=f;h->s[6]+=g;h->s[7]+=hh;
}
static void sha256_update(SHA256*h,const uint8_t*p,size_t n){
  h->n+=n;
  while(n){ size_t k=64-h->c; if(k>n)k=n; memcpy(h->buf+h->c,p,k); h->c+=k; p+=k; n-=k;
    if(h->c==64){ sha256_block(h,h->buf); h->c=0; } }
}
static void sha256_hex(SHA256*h,char*out){
  uint64_t bits=h->n*8; uint8_t pad=0x80; sha256_update(h,&pad,1);
  uint8_t z=0; while(h->c!=56) sha256_update(h,&z,1);
  uint8_t L[8]; int i; for(i=0;i<8;i++) L[i]=(bits>>(56-8*i))&0xff; sha256_update(h,L,8);
  for(i=0;i<8;i++) sprintf(out+i*8,"%08x",h->s[i]);
}
static void sha256_buf(const uint8_t*p,size_t n,char*hex){ SHA256 h; sha256_init(&h); sha256_update(&h,p,n); sha256_hex(&h,hex); }

/* ----------------------------- shim VFS ---------------------------------- */
typedef struct { sqlite3_int64 off; int len; } WR;
typedef struct DedupFile {
  sqlite3_file base;
  sqlite3_file *real;
  int is_main;
  WR *w; int nw, cap;
} DedupFile;

static sqlite3_vfs *g_real = NULL;
static const char *g_store = NULL;
static DedupFile *g_main = NULL;
static long g_bytes = 0;         /* cumulative uncompressed new-page bytes */
static double g_last_ms = 0;     /* latency of the most recent snapshot */
static int g_quiet = 0;          /* suppress per-snapshot print in bench mode */

static int dd_write(sqlite3_file *pf, const void *buf, int amt, sqlite3_int64 off){
  DedupFile *p=(DedupFile*)pf;
  if(p->is_main){
    if(p->nw==p->cap){ p->cap=p->cap?p->cap*2:64; p->w=realloc(p->w,p->cap*sizeof(WR)); }
    p->w[p->nw].off=off; p->w[p->nw].len=amt; p->nw++;
  }
  return p->real->pMethods->xWrite(p->real,buf,amt,off);
}
static int dd_close(sqlite3_file *pf){ DedupFile*p=(DedupFile*)pf; int rc=p->real->pMethods->xClose(p->real); free(p->w); if(p==g_main)g_main=NULL; return rc; }
static int dd_read(sqlite3_file *pf,void*b,int a,sqlite3_int64 o){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xRead(p->real,b,a,o); }
static int dd_trunc(sqlite3_file *pf,sqlite3_int64 s){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xTruncate(p->real,s); }
static int dd_sync(sqlite3_file *pf,int f){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xSync(p->real,f); }
static int dd_size(sqlite3_file *pf,sqlite3_int64*s){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xFileSize(p->real,s); }
static int dd_lock(sqlite3_file *pf,int l){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xLock(p->real,l); }
static int dd_unlock(sqlite3_file *pf,int l){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xUnlock(p->real,l); }
static int dd_crl(sqlite3_file *pf,int*r){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xCheckReservedLock(p->real,r); }
static int dd_fc(sqlite3_file *pf,int op,void*a){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xFileControl(p->real,op,a); }
static int dd_ss(sqlite3_file *pf){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xSectorSize(p->real); }
static int dd_dc(sqlite3_file *pf){ DedupFile*p=(DedupFile*)pf; return p->real->pMethods->xDeviceCharacteristics(p->real); }

static sqlite3_io_methods dd_io = {
  1, dd_close, dd_read, dd_write, dd_trunc, dd_sync, dd_size,
  dd_lock, dd_unlock, dd_crl, dd_fc, dd_ss, dd_dc
};

static int dd_open(sqlite3_vfs*v,const char*z,sqlite3_file*pf,int flags,int*po){
  DedupFile*p=(DedupFile*)pf;
  p->real=(sqlite3_file*)&p[1];
  int rc=g_real->xOpen(g_real,z,p->real,flags,po);
  if(rc==SQLITE_OK){
    p->base.pMethods=&dd_io;
    p->is_main=(flags&SQLITE_OPEN_MAIN_DB)!=0;
    p->w=NULL; p->nw=0; p->cap=0;
    if(p->is_main) g_main=p;
  }
  return rc;
}
/* remaining vfs methods delegate straight to the real vfs */
static int dd_delete(sqlite3_vfs*v,const char*z,int s){ return g_real->xDelete(g_real,z,s); }
static int dd_access(sqlite3_vfs*v,const char*z,int f,int*r){ return g_real->xAccess(g_real,z,f,r); }
static int dd_fullpath(sqlite3_vfs*v,const char*z,int n,char*o){ return g_real->xFullPathname(g_real,z,n,o); }
static void*dd_dlopen(sqlite3_vfs*v,const char*z){ return g_real->xDlOpen(g_real,z); }
static void dd_dlerror(sqlite3_vfs*v,int n,char*o){ g_real->xDlError(g_real,n,o); }
static void(*dd_dlsym(sqlite3_vfs*v,void*h,const char*z))(void){ return g_real->xDlSym(g_real,h,z); }
static void dd_dlclose(sqlite3_vfs*v,void*h){ g_real->xDlClose(g_real,h); }
static int dd_rand(sqlite3_vfs*v,int n,char*o){ return g_real->xRandomness(g_real,n,o); }
static int dd_sleep(sqlite3_vfs*v,int m){ return g_real->xSleep(g_real,m); }
static int dd_time(sqlite3_vfs*v,double*t){ return g_real->xCurrentTime(g_real,t); }
static int dd_lasterr(sqlite3_vfs*v,int n,char*o){ return g_real->xGetLastError(g_real,n,o); }

static sqlite3_vfs dd_vfs;

static void dedup_register(const char*store){
  g_store=store; g_real=sqlite3_vfs_find(0);
  memset(&dd_vfs,0,sizeof dd_vfs);
  dd_vfs.iVersion=1;
  dd_vfs.szOsFile=sizeof(DedupFile)+g_real->szOsFile;
  dd_vfs.mxPathname=g_real->mxPathname;
  dd_vfs.zName="dedup";
  dd_vfs.xOpen=dd_open; dd_vfs.xDelete=dd_delete; dd_vfs.xAccess=dd_access;
  dd_vfs.xFullPathname=dd_fullpath; dd_vfs.xDlOpen=dd_dlopen; dd_vfs.xDlError=dd_dlerror;
  dd_vfs.xDlSym=dd_dlsym; dd_vfs.xDlClose=dd_dlclose; dd_vfs.xRandomness=dd_rand;
  dd_vfs.xSleep=dd_sleep; dd_vfs.xCurrentTime=dd_time; dd_vfs.xGetLastError=dd_lasterr;
  sqlite3_vfs_register(&dd_vfs,0);
}

static int cmp_ll(const void*a,const void*b){ long long x=*(const long long*)a,y=*(const long long*)b; return x<y?-1:x>y?1:0; }
static int cmp_d(const void*a,const void*b){ double x=*(const double*)a,y=*(const double*)b; return x<y?-1:x>y?1:0; }

/* read page size from the main-db header (bytes 16-17) */
static int page_size(DedupFile*p){
  unsigned char h[2]; if(p->real->pMethods->xRead(p->real,h,2,16)!=SQLITE_OK) return 4096;
  int ps=(h[0]<<8)|h[1]; return ps==1?65536:ps;
}

/* snapshot: dedup dirty pages into g_store; returns count of NEW pages */
static int dedup_snapshot(const char*label){
  DedupFile*p=g_main; if(!p) return -1;
  struct timespec t0,t1; clock_gettime(CLOCK_MONOTONIC,&t0);
  int ps=page_size(p);
  /* resolve dirty byte-ranges to a unique sorted page-number list */
  long long*pg=NULL; int npg=0,cap=0;
  for(int i=0;i<p->nw;i++){
    long long first=p->w[i].off/ps, last=(p->w[i].off+p->w[i].len-1)/ps;
    for(long long q=first;q<=last;q++){ if(npg==cap){cap=cap?cap*2:128;pg=realloc(pg,cap*sizeof(long long));} pg[npg++]=q; }
  }
  qsort(pg,npg,sizeof(long long),cmp_ll);
  int newp=0; long long prev=-1;
  unsigned char*buf=malloc(ps); char hex[65], path[1024];
  for(int i=0;i<npg;i++){
    if(pg[i]==prev) continue; prev=pg[i];
    if(p->real->pMethods->xRead(p->real,buf,ps,pg[i]*(sqlite3_int64)ps)!=SQLITE_OK) continue;
    sha256_buf(buf,ps,hex);
    snprintf(path,sizeof path,"%s/objects/%s",g_store,hex);
    struct stat st;
    if(stat(path,&st)!=0){ FILE*f=fopen(path,"wb"); if(f){fwrite(buf,1,ps,f);fclose(f);} newp++; g_bytes+=ps; }
  }
  free(buf); free(pg);
  p->nw=0;  /* clear dirty */
  clock_gettime(CLOCK_MONOTONIC,&t1);
  double ms=(t1.tv_sec-t0.tv_sec)*1e3+(t1.tv_nsec-t0.tv_nsec)/1e6;
  g_last_ms=ms;
  if(!g_quiet) printf("  %-3s new=%-4d snapshot_latency=%.2f ms\n",label,newp,ms);
  return newp;
}

/* ----------------------------- driver ------------------------------------ */
static void run(sqlite3*db,const char*sql){ char*e=0; if(sqlite3_exec(db,sql,0,0,&e)!=SQLITE_OK){ fprintf(stderr,"SQL err: %s\n",e?e:"?"); exit(1);} }

int main(int argc,char**argv){
  const char*store = argc>1?argv[1]:"store_c";
  const char*dbp   = argc>2?argv[2]:"work_c/app.db";
  { char cmd[1200]; snprintf(cmd,sizeof cmd,"rm -rf '%s' '%s'* && mkdir -p '%s/objects' && mkdir -p \"$(dirname '%s')\"",store,dbp,store,dbp); system(cmd); }
  dedup_register(store);

  sqlite3*db;
  if(sqlite3_open_v2(dbp,&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE,"dedup")!=SQLITE_OK){ fprintf(stderr,"open failed\n"); return 1; }
  run(db,"PRAGMA page_size=4096");
  run(db,"PRAGMA journal_mode=DELETE");
  run(db,"CREATE TABLE msgs(id INTEGER PRIMARY KEY, ts INT, body TEXT)");
  run(db,"BEGIN");
  sqlite3_stmt*st; sqlite3_prepare_v2(db,"INSERT INTO msgs VALUES(?,?,?)",-1,&st,0);
  char body[64];
  for(int i=0;i<20000;i++){
    snprintf(body,sizeof body,"message %d with padding text to fill the page",i);
    sqlite3_bind_int(st,1,i); sqlite3_bind_int(st,2,i*1000); sqlite3_bind_text(st,3,body,-1,SQLITE_TRANSIENT);
    sqlite3_step(st); sqlite3_reset(st);
  }
  sqlite3_finalize(st); run(db,"COMMIT");

  /* --- bench mode: many timed incremental snapshots -> latency distribution --- */
  if(argc>3 && strcmp(argv[3],"bench")==0){
    int N = argc>4?atoi(argv[4]):100;
    g_quiet=1;
    dedup_snapshot("seed");                 /* seed the store (full DB) */
    double*lat=malloc(N*sizeof(double)); char q[160];
    for(int k=0;k<N;k++){
      snprintf(q,sizeof q,"UPDATE msgs SET body='iter %d unique text' WHERE id IN (%d,%d,%d)",
               k,(k*7)%20000,(k*13)%20000,(k*29)%20000);
      run(db,q);
      dedup_snapshot("it");
      lat[k]=g_last_ms;
    }
    qsort(lat,N,sizeof(double),cmp_d);
    double sum=0; for(int i=0;i<N;i++) sum+=lat[i];
    printf("INCREMENTAL SNAPSHOT LATENCY over %d iterations (3-row update each):\n",N);
    printf("  min=%.3f  p50=%.3f  p90=%.3f  p99=%.3f  max=%.3f  mean=%.3f  ms\n",
           lat[0],lat[N/2],lat[(int)(N*0.9)],lat[(int)(N*0.99)],lat[N-1],sum/N);
    free(lat); sqlite3_close(db); return 0;
  }

  /* --- energy mode: run backups continuously for T seconds (bounded store) --- */
  if(argc>3 && strcmp(argv[3],"energy")==0){
    double secs = argc>4?atof(argv[4]):120.0;
    int do_shot = !(argc>5 && strcmp(argv[5],"noshot")==0);  /* control window */
    g_quiet=1;
    if(do_shot) dedup_snapshot("seed");
    struct timespec t0,tn; clock_gettime(CLOCK_MONOTONIC,&t0);
    long iters=0; char q[128];
    for(;;){
      /* update a FIXED small set of rows to CYCLING content so page contents
         recur -> store stays bounded, but the full backup compute path (read
         dirty pages + sha256 + dedup check) runs every iteration. */
      snprintf(q,sizeof q,"UPDATE msgs SET body='state_%ld' WHERE id IN (1,2,3,4,5)",iters%64);
      run(db,q);
      if(do_shot) dedup_snapshot("e");
      else if(g_main) g_main->nw=0;   /* control: drop dirty list (no snapshot) */
      iters++;
      if((iters&1023)==0){
        clock_gettime(CLOCK_MONOTONIC,&tn);
        double el=(tn.tv_sec-t0.tv_sec)+(tn.tv_nsec-t0.tv_nsec)/1e9;
        if(el>=secs){ printf("ENERGY-MODE: %ld backups in %.1f s (%.0f backups/s)\n",iters,el,iters/el); break; }
      }
    }
    sqlite3_close(db); return 0;
  }

  char cp[1400];
  #define SNAPCOPY(L) do{ snprintf(cp,sizeof cp,"cp '%s' \"$(dirname '%s')/%s.db\"",dbp,dbp,L); system(cp);}while(0)

  printf("=== C shim (per snapshot) ===\n");
  dedup_snapshot("s0"); SNAPCOPY("s0");
  run(db,"UPDATE msgs SET body='EDITED a' WHERE id IN (10,500,1000)");
  dedup_snapshot("s1"); SNAPCOPY("s1");
  run(db,"BEGIN");
  sqlite3_prepare_v2(db,"INSERT INTO msgs VALUES(?,?,?)",-1,&st,0);
  for(int i=0;i<50;i++){ snprintf(body,sizeof body,"appended row %d",i);
    sqlite3_bind_int(st,1,20000+i); sqlite3_bind_int(st,2,i); sqlite3_bind_text(st,3,body,-1,SQLITE_TRANSIENT);
    sqlite3_step(st); sqlite3_reset(st); }
  sqlite3_finalize(st); run(db,"COMMIT");
  run(db,"UPDATE msgs SET body='EDITED b' WHERE id=7");
  dedup_snapshot("s2"); SNAPCOPY("s2");
  sqlite3_close(db);
  printf("cumulative new-page bytes: %ld\n",g_bytes);
  return 0;
}