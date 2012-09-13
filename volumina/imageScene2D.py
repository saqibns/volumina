import numpy

import volumina
from volumina.colorama import Fore, Back, Style

from functools import partial
from PyQt4.QtCore import QRect, QRectF, QMutex, QPointF, Qt, QSizeF
from PyQt4.QtGui import QGraphicsScene, QImage, QTransform, QPen, QColor, QBrush, \
                        QFont, QPainter, QGraphicsItem

from imageSceneRendering import ImageSceneRenderThread

from volumina.tiling import Tiling, TileProvider, TiledImageLayer
from volumina.layerstack import LayerStackModel
from volumina.pixelpipeline.imagepump import StackedImageSources
import math

import threading
import collections

#*******************************************************************************
# I m a g e S c e n e 2 D                                                      *
#*******************************************************************************
class DirtyIndicator(QGraphicsItem):
    """
    Indicates the computation progress of each tile. Each tile can be composed
    of multiple layers and is dirty as long as any of these layer tiles are
    not yet computed/up to date. The number of layer tiles still missing is
    indicated by a 'pie' chart.
    """
    def __init__(self, tiling):
        QGraphicsItem.__init__(self, parent=None)
        self._tiling = tiling
        self._indicate = numpy.zeros(len(tiling))
        self._indicateDelayCounter = numpy.zeros(len(tiling))

    def boundingRect(self):
        return self._tiling.boundingRectF()
    
    def paint(self, painter, option, widget):
        dirtyColor = QColor(255,0,0)
        doneColor  = QColor(0,255 ,0)
        painter.setOpacity(0.5)
        painter.save()
        painter.setBrush(QBrush(dirtyColor, Qt.SolidPattern))
        painter.setPen(dirtyColor)

        for i,p in enumerate(self._tiling.tileRectFs):
            if self._indicate[i] == 1.0:
                continue
            if self._indicate[i] == 0.0 and self._indicateDelayCounter[i] < 2:
                continue
            w,h = p.width(), p.height()
            r = min(w,h)
            rectangle = QRectF(p.center()-QPointF(r/4,r/4), QSizeF(r/2, r/2));
            startAngle = 0 * 16
            spanAngle  = min(360*16, int((1.0-self._indicate[i])*360.0) * 16)
            painter.drawPie(rectangle, startAngle, spanAngle)

        painter.restore()

    def setTileProgress(self, tileId, progress):
        self._indicate[tileId] = progress
        if progress == 0.0:
            self._indicateDelayCounter[tileId] += 1
        else:
            self._indicateDelayCounter[tileId] = 0
        self.update()

#*******************************************************************************
# I m a g e S c e n e 2 D                                                      *
#*******************************************************************************

class ImageScene2D(QGraphicsScene):
    """
    The 2D scene description of a tiled image generated by evaluating
    an overlay stack, together with a 2D cursor.
    """

    @property
    def stackedImageSources(self):
        return self._stackedImageSources
    
    @stackedImageSources.setter
    def stackedImageSources(self, s):
        self._stackedImageSources = s
        s.sizeChanged.connect(self._onSizeChanged)

    @property
    def showTileOutlines(self):
        return self._showTileOutlines
    @showTileOutlines.setter
    def showTileOutlines(self, show):
        self._showTileOutlines = show
        self.invalidate()

    @property
    def sceneShape(self):
        """
        The shape of the scene in QGraphicsView's coordinate system.
        """
        return (self.sceneRect().width(), self.sceneRect().height())
    @sceneShape.setter
    def sceneShape(self, sceneShape):
        """
        Set the size of the scene in QGraphicsView's coordinate system.
        sceneShape -- (widthX, widthY),
        where the origin of the coordinate system is in the upper left corner
        of the screen and 'x' points right and 'y' points down
        """   
        assert len(sceneShape) == 2
        self.setSceneRect(0,0, *sceneShape)
        
        #The scene shape is in Qt's QGraphicsScene coordinate system,
        #that is the origin is in the top left of the screen, and the
        #'x' axis points to the right and the 'y' axis down.
        
        #The coordinate system of the data handles things differently.
        #The x axis points down and the y axis points to the right.

        r = self.scene2data.mapRect(QRect(0,0,sceneShape[0], sceneShape[1]))
        sliceShape = (r.width(), r.height())
        
        if self._dirtyIndicator:
            self.removeItem(self._dirtyIndicator)
        del self._dirtyIndicator
        self._dirtyIndicator = None

        self._tiling = Tiling(sliceShape, self.data2scene)

        self._dirtyIndicator = DirtyIndicator(self._tiling)
        self.addItem(self._dirtyIndicator)

        self._onSizeChanged()
        if self._tileProvider:
            self._tileProvider.notifyThreadsToStop() # prevent ref cycle

        self._tileProvider = TileProvider(self._tiling, self._stackedImageSources)
        self._tileProvider.changed.connect(self.invalidateViewports)

    def setCacheSize(self, cache_size):
        if cache_size != self._tileProvider._cache_size:
            self._tileProvider = TileProvider(self._tiling, self._stackedImageSources, cache_size=cache_size)
            self._tileProvider.changed.connect(self.invalidateViewports)

    def invalidateViewports( self, rectF ):
        '''Call invalidate on the intersection of all observing viewport-rects and rectF.'''
        rectF = rectF if rectF.isValid() else self.sceneRect()
        for view in self.views():
            QGraphicsScene.invalidate( self, rectF.intersected(view.viewportRect()) )        

    def __init__( self, parent=None ):
        QGraphicsScene.__init__( self, parent=parent )

        self.data2scene = QTransform(0,1,1,0,0,0) 
        self.scene2data = self.data2scene.transposed()

        self._tiling = Tiling((0,0), self.data2scene)
        self._brushingLayer  = TiledImageLayer(self._tiling)
        self._dirtyIndicator = DirtyIndicator(self._tiling)
        self.addItem(self._dirtyIndicator)
        self._stackedImageSources = StackedImageSources( LayerStackModel() )
        self._tileProvider = TileProvider( self._tiling, self._stackedImageSources)
        self._showTileOutlines = False

    def __del__( self ):
        if self._tileProvider:
            self._tileProvider.notifyThreadsToStop()
    
    def drawLine(self, fromPoint, toPoint, pen):
        tileId = self._tiling.containsF(toPoint)
        if tileId is None:
            return
       
        p = self._brushingLayer[tileId] 
        p.lock()
        painter = QPainter(p.image)
        painter.setPen(pen)
        
        tL = self._tiling.imageRectFs[tileId].topLeft()
        painter.drawLine(fromPoint-tL, toPoint-tL)
        painter.end()
        p.dataVer += 1
        p.unlock()
        self.scheduleRedraw(self._tiling.imageRectFs[tileId])

    def _onSizeChanged(self):
        self._brushingLayer  = TiledImageLayer(self._tiling)
                
    def drawForeground(self, painter, rect):
        if self._tiling is None:
            return

        tile_nos = self._tiling.intersectedF(rect)

        for tileId in tile_nos:
            p = self._brushingLayer[tileId]
            if p.dataVer == p.imgVer:
                continue

            p.paint(painter) #access to the underlying image patch is serialized

            ## draw tile outlines
            if self._showTileOutlines:
                # Dashed black line
                pen = QPen()
                pen.setDashPattern([5,5])
                painter.setPen( pen )
                painter.drawRect(self._tiling.imageRects[tileId])

                # Dashed white line 
                # (offset to occupy the spaces in the dashed black line)
                pen = QPen()
                pen.setDashPattern([5,5])
                pen.setDashOffset(5)
                pen.setColor( QColor(Qt.white) )
                painter.setPen( pen )
                painter.drawRect(self._tiling.imageRects[tileId])
    
    def indicateSlicingPositionSettled(self, settled):
        self._dirtyIndicator.setVisible(settled)
   
    def drawBackground(self, painter, rectF):
        if self._tileProvider is None:
            return

        tiles = self._tileProvider.getTiles(rectF)
        for tile in tiles:
            # prevent flickering
            if not tile.progress < 1.0:
                painter.drawImage(tile.rectF, tile.qimg)
            self._dirtyIndicator.setTileProgress(tile.id, tile.progress) 

    def joinRendering( self ):
        return self._tileProvider.join()
