import numpy as np
import skimage.feature
import skimage.color
import skimage.transform
import caffe
import tempfile
import re

class ColorHist:
    def set_params(self, bins=4):
        self.bins = bins

    def extract(self, img):
        pixels = np.reshape(img, (img.shape[0]*img.shape[1],-1))
        hist,e = np.histogramdd(pixels, bins=self.bins, range=3*[[0,255]], normed=True)
        hist = np.reshape(hist, (-1)) # Make it 1-D
        return hist


class HoGDalal:
    def set_params(self, ori=9, px_per_cell=(8,8), cells_per_block=(2,2), window_size=40):
        self.ori = ori
        self.px_per_cell = px_per_cell
        self.cells_per_block = cells_per_block
        self.window_size = window_size

    def extract(self, img):
        flat_img = flatten(img)
        flat_img = skimage.transform.resize(img[:,:,1], (self.window_size, self.window_size))
        hog_feat = skimage.feature.hog(flat_img, orientations=self.ori, pixels_per_cell=self.px_per_cell,
                                       cells_per_block=self.cells_per_block)
        return hog_feat

class TinyImage:
    def set_params(self, flatten=False):
        self.flatten = flatten

    def extract(self, img):
        if self.flatten:
            img = flatten(img)

        tiny = skimage.transform.resize(img, (32,32))
        tiny = np.reshape(tiny, (-1))
        return tiny

class CNN:

    max_batch_size = 500

    def initialize_cnn(self, batch_size):
        temp = tempfile.NamedTemporaryFile()
        def_path = "caffemodels/" + self.model +"/train.prototxt"
        weight_path = "caffemodels/" + self.model + "/weights.caffemodel"
        #go through and edit batch size
        arch = open(def_path,'r').readlines()
        for i in range(len(arch)):
            if "batch_size" in arch[i]:
                arch[i] = re.sub('\d+',str(batch_size),arch[i])
            if "height" in arch[i]:
                self.h = re.findall('\d+',arch[i])[0]
            if "width" in arch[i]:
                self.w = re.findall('\d+',arch[i])[0]
        temp.writelines(arch)
        temp.seek(0)
        self.net = caffe.Net(temp.name,weight_path,caffe.TEST)
        self.transformer = caffe.io.Transformer({'data': self.net.blobs['data'].data.shape})
        self.transformer.set_transpose('data', self.transpose)
        self.transformer.set_channel_swap('data',self.channel_swap)
        temp.close()

    def set_params(self, model = "caffenet", layer_name = "fc7", transpose = (2,0,1), channel_swap = (2,1,0)):
        '''
        Parameters
        ------------
        "model" is the folder name where the model specs and weights live. 
        ie model = "VGG", "GoogleNet", "BVLC_Reference_Caffenet"
        
        "layer_name" is the layer name used for extraction 
        ie layer_name = "fc7" (for VGG)
        
        see below for better idea of what "transpose" and "channel_swap" are used for
        http://nbviewer.jupyter.org/github/BVLC/caffe/blob/master/examples/00-classification.ipynb
        '''
        self.model = model
        self.layer_name = layer_name
        self.transpose = transpose
        self.channel_swap = channel_swap
        
    #assume that we're getting a single image
    #Img comes in format (x,y,c)
    def extract(self, img):
        self.initialize_cnn(1)
        img = img[0:int(self.w),0:int(self.h)]
        img = self.transformer.preprocess('data',img)
        if len(img.shape) == 3:
            img = np.expand_dims(img,axis=0)
        self.net.set_input_arrays(img, np.array([1],dtype=np.float32))
        p = self.net.forward()
        return self.net.blobs[self.layer_name].data[...].reshape(-1)
    #expecting an array of images
    def extract_many(self, img):
        codes = np.array([])
        if img.shape[0] > self.max_batch_size:
            print 'exceeded max batch size. splitting into minibatches'
            self.initialize_cnn(self.max_batch_size)
            for i in range(int(np.round(img.shape[0]/self.max_batch_size))):
                print 'minibatch: ' + str(i)
                tim = img[i*500:(i+1)*500,0:int(self.w),0:int(self.h)]

                #Lots of repeated code
                tim = np.array([self.transformer.preprocess('data',i) for i in tim])
                self.net.set_input_arrays(tim, np.ones(self.max_batch_size,dtype=np.float32))
                p = self.net.forward()
                codes = np.append(codes,self.net.blobs[self.layer_name].data[...])
            if np.round(img.shape[0]/self.max_batch_size) * self.max_batch_size < img.shape[0]:
                mult = np.round(img.shape[0]/self.max_batch_size) * self.max_batch_size
                print 'final minibatch'
                self.initialize_cnn(img.shape[0]-mult)
                tim = img[mult:img.shape[0],0:int(self.w),0:int(self.h)]

                #Lots of repeated code
                tim = np.array([self.transformer.preprocess('data',i) for i in tim])
                self.net.set_input_arrays(tim, np.ones(img.shape[0]-mult,dtype=np.float32))
                p = self.net.forward()
                codes = np.append(codes,self.net.blobs[self.layer_name].data[...])
            codes = codes.reshape(np.append(-1,self.net.blobs[self.layer_name].data.shape[1:]))
        else:
            self.initialize_cnn(img.shape[0])
            img = img[:,0:int(self.w),0:int(self.h)]
            img = np.array([self.transformer.preprocess('data',i) for i in img])
            self.net.set_input_arrays(img, np.ones(img.shape[0],dtype=np.float32))
            p = self.net.forward()
            codes = self.net.blobs[self.layer_name].data[...]
        return codes

def flatten(img):
    if img.shape[2] > 1:
        Y = 0.2125*img[:,:,0] + 0.7154*img[:,:,1] + 0.0721*img[:,:,2]
    else:
        Y = img
    return Y

kinds = [ColorHist, HoGDalal, TinyImage, CNN]
