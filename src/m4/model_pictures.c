/* Experimental HX Stomp 3.80 UI overlay. No storage, USB or DSP writes.
 * The host supplies exact resource indices and reserves zeroed SDRAM state.
 * Stock VIEW opens Edit; artwork is drawn on that default screen.
 * All model identities are preset IDs, never displayed strings.
 */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;
typedef int i32;
#define W(a) (*(volatile u32 *)(a))
#define B(a) (*(volatile u8 *)(a))
#define F(addr,ret,...) ((ret(*)(__VA_ARGS__))((addr)|1))
#define SCREEN 0x28128260u
#define ACTIVE_PANEL 0x28128258u
#define EDIT_PANEL 2u
#define EDIT 0x281282f8u
#define EDIT_PARAMS 0x28128270u
#define CHANNEL_WIDGET 0x281f72b0u
#define LABELS 0x281f7370u
#define DETAIL_TITLE (LABELS+0x30u)
#define VALUES 0x281f76a8u
#define PAGE_SEGMENT 0x281f7220u
#define PAGE_COUNT 0x281f79a4u
#define PARAM_SEPARATOR_Y 90
#define TITLE_Y 212
#define TITLE_HEIGHT 18
#define STEREO_GAP 7
#define STEREO_Y (TITLE_Y-1)
#define GRAPH_TOP_Y (TITLE_Y-TITLE_HEIGHT)
typedef struct {u32 made;u32 image[9];u32 node[10];} State;
typedef struct {int top,bottom,shift;} GraphLayout;
typedef struct {u16 compact;u8 height,compact_height;} PictureLayout;
typedef struct {u16 plain,plain_width,colon,colon_width;char label[10];} CategoryStyle;
enum {OUTPUT_CATEGORY=20};
#define S ((volatile State *)STATE_ADDRESS)

static int pictures_enabled(void){
    u32 setting[2]={0,MODEL_PICTURES_DEFAULT};
    int status=F(GET_SETTING,int,u32,u32*)(MODEL_PICTURES_SETTING,setting);
    u32 value=status<0||setting[1]>MODEL_PICTURES_MAX?MODEL_PICTURES_DEFAULT:setting[1];
    return value!=MODEL_PICTURES_OFF;
}

static void lock(void){F(0x28e24970,void,void)();}
static void unlock(void){F(0x28e24ae8,void,void)();}
static void fill(u32 fb,i32 x,i32 y,i32 w,i32 h,u32 color,u32 anchor){
    if(w>0&&h>0&&w<=320&&h<=240)
        F(0x100017e8,void,u32,i32,i32,i32,i32,u32,u32)(fb,x,y,w,h,color,anchor);
}
static int length(const char *s,int max){int n=0;if(!s)return 0;while(n<max&&s[n])n++;return n;}
static void text(u32 fb,const char *s,int x,int y,u32 color,int anchor,int max){
    char clean[65];int n=length(s,max>64?64:max);
    for(int i=0;i<n;i++){u8 c=s[i];clean[i]=(c<32||c>126)?' ':c;}
    clean[n]=0;
    if(n)F(0x28db7aa4,void,u32,const char*,int,int,int,int,u32,int,int)(fb,clean,n,x,y,1,color,anchor,1);
}
static const char *widget_string(u32 widget){
    u32 ptr=W(widget);
    if(B(widget+0x2a)&&ptr>=0x10000000u&&ptr<0x30000000u&&B(ptr))
        return (const char*)ptr;
    return (const char*)0;
}
static void widget_text(u32 fb,u32 widget,int x,int y,int anchor,int max){
    const char *s=widget_string(widget);
    if(s)text(fb,s,x,y,W(widget+0x1c),anchor,max);
}
static int y_graph(int y){return y+12;}
static void image(u32 fb,u32 index,int x,int y,int anchor){
    if(!index||index>=IMAGE_COUNT)return;
    if(!S->made){
        F(0x28e24ab0,void,u32,u32)((u32)S->image,0);
        S->made=1;
    }
    F(0x28e24aa8,void,u32,int)((u32)S->image,anchor);
    F(0x28e24c18,void,u32,u32)((u32)S->image,index);
    F(0x28e24c68,void,u32,int,int)((u32)S->image,x,y);
    F(0x28e24a98,void,u32,int)((u32)S->image,1);
    F(0x28e24a00,void,u32,u32,u32)(fb,fb,(u32)S->image);
}
static const u16 *heading_glyph(u8 c){
    if(c<32||c>126)c=' ';
    return (const u16*)TITLE_GLYPHS+2*(c-32);
}
static int heading_text(u32 fb,const char *str,int n,int x){
    for(int i=0;i<n;i++){
        const u16 *glyph=heading_glyph(str[i]);
        image(fb,glyph[0],x,TITLE_Y,6);
        x+=glyph[1];
    }
    return x;
}
static int same_title(const char *uppercase,const char *name){
    if(!name)return 0;
    while(*uppercase&&*name){
        u8 c=*name++;
        if(c>='a'&&c<='z')c-='a'-'A';
        if(c!=*uppercase++)return 0;
    }
    return !*uppercase&&!*name;
}
static void block_title(u32 fb,u32 pid,u8 category){
    const char *name=widget_string(LABELS);
    if(pid==NAM_PID){
        /* The donor's IR title hook updates a separate text widget. */
        const char *capture=widget_string(DETAIL_TITLE);
        if(capture&&capture[0]!='<')name=capture;
    }
    if(pid<PID_COUNT){
        /* Edit widgets can contain the stock alias even when HXtra renamed the model. */
        int renamed=((const u8*)PREFER_HXTRA_NAME)[pid];
        if(!name||renamed||(pid==INPUT_PID&&name[0]=='A'&&!name[1])){
            u16 offset=((const u16*)NAME_OFFSETS)[pid];
            name=(const char*)(MODEL_NAMES+offset);
        }
        u8 override=((const u8*)CATEGORY_OVERRIDES)[pid];
        if(override)category=override;
    }
    const CategoryStyle *style=(const CategoryStyle*)CATEGORY_STYLES+
                               (category<CATEGORY_COUNT?category:0);
    /* The stock inspector writes the active route to its detail widget. */
    const char *route=0;
    if(pid==INPUT_PID||category==OUTPUT_CATEGORY)
        route=widget_string(DETAIL_TITLE);
    if(route)name=route;
    /* Use the same channel indicator as Edit, including its bypassed state. */
    u32 channel_image=W(CHANNEL_WIDGET);
    int stereo=B(CHANNEL_WIDGET+0x1d)&&
               (channel_image==STEREO_IMAGE||channel_image==STEREO_OFF_IMAGE);
    int badge_width=stereo?STEREO_GAP+STEREO_WIDTH:0;
    int n=length(name,32);
    if(style->plain&&(!n||same_title(style->label,name))){
        int x=(320-style->plain_width-badge_width)/2;
        image(fb,style->plain,x,TITLE_Y,6);
        if(stereo)image(fb,STEREO_BADGE,x+style->plain_width+STEREO_GAP,STEREO_Y,6);
        return;
    }
    int max_width=308-badge_width;
    int prefix_width=!route&&style->colon?style->colon_width+7:0;
    int width=prefix_width,visible=0;
    while(visible<n&&width+heading_glyph(name[visible])[1]<=max_width)
        width+=heading_glyph(name[visible++])[1];
    int abbreviated=visible<n;
    if(abbreviated){
        int ellipsis_width=3*heading_glyph('.')[1];
        while(visible&&width+ellipsis_width>max_width)
            width-=heading_glyph(name[--visible])[1];
        width+=ellipsis_width;
    }
    int x=(320-width-badge_width+(stereo?0:2))/2;
    if(prefix_width)image(fb,style->colon,x,TITLE_Y,6);
    x=heading_text(fb,name,visible,x+prefix_width);
    if(abbreviated)x=heading_text(fb,"...",3,x);
    if(stereo)image(fb,STEREO_BADGE,x+STEREO_GAP,STEREO_Y,6);
}
static void graph_bounds(GraphLayout *layout,int y,int h,int anchor){
    if(h<=0||h>100||anchor>8)return;
    int bottom=y-(anchor/3)*h/2;
    if(bottom<layout->bottom)layout->bottom=bottom;
    if(bottom+h>layout->top)layout->top=bottom+h;
}
static void graph_image(u32 fb,u32 widget,GraphLayout *layout,int draw){
    if(!B(widget+0x1d))return;
    u32 idx=W(widget);int y=W(widget+0x10),x=W(widget+0xc);
    if(idx>=SMALL_COUNT||y<125||y>210||x<0||x>320)return;
    u32 small=((const u32*)SMALL_TABLE)[idx];
    if(small){
        int anchor=B(widget+0x1c),h=W(widget+0x18);
        y=y_graph(y);
        if(draw)image(fb,small,x,y-layout->shift,anchor);
        else graph_bounds(layout,y,h,anchor);
    }
}
static void graph_rect(u32 fb,u32 widget,GraphLayout *layout,int draw){
    if(!B(widget+0x15))return;
    int x=W(widget+4),y=W(widget+8),w=W(widget+12),h=W(widget+16);
    int anchor=B(widget+0x14);
    if(y<125||y>210||x<0||x>320||w<=0||w>320||h<=0||h>100||anchor>8)return;
    y=y_graph(y);
    if(draw)fill(fb,x,y-layout->shift,w,h,W(widget),anchor);
    else graph_bounds(layout,y,h,anchor);
}
static void graph_widgets(u32 fb,GraphLayout *layout,int draw){
    const u16 positions[]={0x10c,0x290,0x2b4,0x328,0x34c,0x370,0x40c,0x430,0x704};
    graph_rect(fb,EDIT+0x208,layout,draw);
    graph_rect(fb,EDIT+0x224,layout,draw);
    for(int i=0;i<9;i++)graph_image(fb,EDIT+positions[i],layout,draw);
    for(int i=0;i<8;i++)graph_image(fb,EDIT+0x4a4+0x24*i,layout,draw);
}
static u32 selected_pid(u8 *category){
    u8 path=0,info[0x80];
    u32 block=F(0x28df0280,u32,u8*)(&path);
    if(block==0xffffffffu)return PID_COUNT;
    u32 dsp=F(0x28d92a98,u32,void)();
    info[1]=info[2]=0xff;
    info[12]=0;
    if(F(0x28e0fe94,int,u32,u32,void*)(dsp,block,info)<0)return PID_COUNT;
    *category=info[12];
    return info[1]|((u32)info[2]<<8);
}
/* Read DSP state once, outside the widget lock. No DSP calls in drawing. */
static int block_cost(u32 dsp,u32 slot){
    u32 handle=W(dsp+0x14+0x54*slot);
    if(handle>0x4b)return -1;
    u32 nodes=W(dsp+0x4400);if(!nodes)return -1;
    u32 node=W(nodes+0x24+4*handle);
    if(!node||B(node+5)!=1)return -1;
    return (W(node+0x44)+50)/100;
}
static int costs(int out[8]){
    for(int i=0;i<8;i++)out[i]=-1;
    u32 dsp=W(0x28449e78u);if(!dsp)return -1;
    F(0x28e07844,void,int)(0);
    int sum=0;
    for(int i=0;i<16;i++){int n=block_cost(dsp,i);if(n>=0)sum+=n;}
    for(int i=0;i<8;i++)out[i]=block_cost(dsp,i+(B(0x2812c590u+0x3f+i)?8:0));
    F(0x28e0785c,void,int)(0);
    return sum;
}
static int digits(char *out,int value){
    int n=0;if(value>999)value=999;
    if(value>=100)out[n++]='0'+value/100;
    if(value>=10)out[n++]='0'+(value/10)%10;
    out[n++]='0'+value%10;out[n]=0;return n;
}
/* Same 3x5 digit shapes as the donor DSP meter, at native two-pixel scale. */
static void tiny_number(u32 fb,int value,int cx,int cy,u32 color){
    static const u16 font[]={0x7b6f,0x2c97,0x73e7,0x73cf,0x5bc9,0x79cf,0x79ef,0x7249,0x7bef,0x7bcf};
    if(value<0)return;
    char number[4];int n=digits(number,value),x=cx-(n*8-2)/2;
    for(int k=0;k<n;k++){
        u16 bits=font[number[k]-'0'];
        for(int row=0;row<5;row++)for(int col=0;col<3;col++)
            if(bits&(1u<<(14-row*3-col)))fill(fb,x+8*k+2*col,cy+3-2*row,2,2,color,0);
    }
}
static void total(u32 fb,int value){
    if(value<0)return;
    char number[12];int n=digits(number,value);
    number[n++]='/';number[n++]='7';number[n++]='5';number[n]=0;
    text(fb,number,317,235,0xffffff,8,11);
}

static void page_bar(u32 fb){
    if(W(PAGE_COUNT)<2||!B(PAGE_SEGMENT+0x15))return;
    u32 x=W(PAGE_SEGMENT+4),w=W(PAGE_SEGMENT+12);
    if(x>=320||!w||w>320-x)return;
    int start=x*77/320,end=(x+w)*77/320;
    fill(fb,236,87,77,1,0x555555,0);
    fill(fb,236+start,87,end>start?end-start:1,2,W(PAGE_SEGMENT),0);
}

/* Runs after BOTH paints used by Edit. It builds a complete screen from
 * original widget state; it never repeatedly rescales already painted pixels. */
void mp_edit(u32 fb,u32 view){
    /* Menu and other panels can replace the parameters with Edit still active. */
    if(B(SCREEN)!=1||B(ACTIVE_PANEL)!=EDIT_PANEL)return;
    if(view!=EDIT&&view!=EDIT_PARAMS)return;
    if(!pictures_enabled())return;
    u8 category=0;
    u32 pid=selected_pid(&category); /* DSP lookup before acquiring widget lock */
    u32 pic=pid<PID_COUNT?((const u32*)PID_TABLE)[pid]:0;
    if(pid==NAM_PID)pic=NAM_EDIT_IMAGE;
    int slot_cost[8];int sum=costs(slot_cost);
    lock();fill(fb,0,0,320,240,0,0);
    widget_text(fb,EDIT+0x5c,4,237,6,4);
    widget_text(fb,EDIT+0x8c,46,237,6,16);
    block_title(fb,pid,category);
    GraphLayout layout={0,240,0};
    graph_widgets(fb,&layout,0);
    if(layout.top>GRAPH_TOP_Y)layout.shift=layout.top-GRAPH_TOP_Y;
    graph_widgets(fb,&layout,1);
    for(int i=0;i<8;i++){
        u32 widget=EDIT+0x4a4+0x24*i;
        if(B(widget+0x1d)&&W(widget)!=0x67){
            int w=W(widget+0x14),h=W(widget+0x18),anchor=B(widget+0x1c);
            int x=W(widget+0xc)-(anchor%3)*w/2+w/2;
            int y=y_graph(W(widget+0x10))-layout.shift-(anchor/3)*h/2+h-(w<h?w:h)/2;
            tiny_number(fb,slot_cost[i],x,y,0);
        }
    }
    if(pic){
        PictureLayout art=((const PictureLayout*)PICTURE_LAYOUT)[pid];
        int graph_bottom=layout.top?layout.bottom-layout.shift:164;
        int space=graph_bottom-PARAM_SEPARATOR_Y-5;
        if(art.height>space){
            pic=art.compact;
            art.height=art.compact_height;
        }
        if(art.height<=space)
            image(fb,pic,160,(graph_bottom+PARAM_SEPARATOR_Y+1)/2,4);
    }
    fill(fb,0,PARAM_SEPARATOR_Y,320,1,0x777777,0);
    page_bar(fb);
    for(int i=0;i<3;i++){
        widget_text(fb,VALUES+0x30*i,53+107*i,76,7,16);
        widget_text(fb,LABELS+0x90+0x30*i,53+107*i,42,7,16);
        fill(fb,7+107*i,52,94,2,0x555555,0);
    }
    total(fb,sum);unlock();
}

/* Browser: redraw the five existing rows into a bounded left pane and draw
 * the selected model's picture in a separate right pane. No model is loaded
 * by the preview. At category/favourites levels the original view is kept. */
void mp_browser(u32 fb,u32 view){
    if(B(view+0x40)!=2)return;
    if(!pictures_enabled())return;
    u8 path=0;u32 desc=0,pic=0;
    F(0x28df0280,u32,u8*)(&path);
    if(path>1)return;
    u32 category=B(view + 0x56e + path),sub=B(view + 0x570 + path);
    u32 index=W(view+0x574+4*path);
    int count=F(0x28e0edcc,int,u32,u32)(category,sub);
    if(count<=0||index>=(u32)count)return;
    if(F(0x28e01204,int,u32,u32,u32,u32*)(category,sub,index,&desc)>=0&&desc){
        u32 model_index=W(desc+4);
        u32 pid=model_index<MODEL_INDEX_COUNT?((const u16*)INDEX_PID_TABLE)[model_index]:0xffff;
        if(pid<PID_COUNT)pic=((const u32*)PID_TABLE)[pid];
    }
    u32 row_colors[5];int row_cost[5];
    for(int i=0;i<5;i++){
        u32 item=0;row_colors[i]=0x666666;row_cost[i]=-1;
        if((u32)B(view+0x569)+i>=(u32)count)continue;
        if(F(0x28e01204,int,u32,u32,u32,u32*)(category,sub,B(view+0x569)+i,&item)>=0&&item)
        {
            row_colors[i]=B(item+0x28)?0xffffff:0x666666;
            int cost=0;
            if(F(0x28e01d50,int,u32,int*)(item,&cost)>=0)row_cost[i]=(cost+50)/100;
        }
    }
    lock();fill(fb,0,0,320,202,0,0);
    /* The header above row202 remains the stock header and DSP total. */
    for(int i=0;i<5;i++){
        u32 iw=view+0x22c+0x24*i;
        if(!B(iw+0x1d)||(u32)B(view+0x569)+i>=(u32)count)continue;
        u32 ix=W(iw);
        if(ix<SMALL_COUNT){u32 si=((const u32*)SMALL_TABLE)[ix];if(si)image(fb,si,17,187-40*i,4);}
        const char *name=(const char*)(view+0x3d0+0x11*i);
        int row=(int)index-(int)B(view+0x569);
        if(row==i)fill(fb,58,169-40*i,164,34,0x222222,0);
        tiny_number(fb,row_cost[i],42,182-40*i,0xffffff);
        text(fb,name,60,194-40*i,row_colors[i],6,14);
    }
    fill(fb,225,0,95,202,0,0);
    fill(fb,225,0,1,197,0x444444,0);
    image(fb,pic,272,120,4);
    unlock();
}
